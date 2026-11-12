import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

// Run from any directory with Node 22+ and Edge or Chrome installed.
// Set CHROMIUM_BIN to override browser discovery.
const windowsPrograms = [process.env["ProgramFiles(x86)"], process.env.ProgramFiles].filter(Boolean);
const executable = process.env.CHROMIUM_BIN ?? [
    ...windowsPrograms.flatMap(root => [
        join(root, "Microsoft/Edge/Application/msedge.exe"),
        join(root, "Google/Chrome/Application/chrome.exe"),
    ]),
    "/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge",
].find(existsSync);
assert.ok(executable, "Install Chromium or set CHROMIUM_BIN");

const web = fileURLToPath(new URL("./ui/web/", import.meta.url));
const contentTypes = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html" };
const requests = [];
const today = new Date();
const bookingDate = [
    today.getFullYear(), String(today.getMonth() + 1).padStart(2, "0"), "15",
].join("-");
const review = {
    action_id: "action-a", type: "create_booking",
    service_name: "Dental checkup", service_name_ar: "فحص الأسنان",
    provider_name: "Dr. Rania", provider_name_ar: "د. رانيا",
    patient_name: "Lina Mansour", masked_phone: "••••1234",
    date: bookingDate, start: "15:30",
};
const booking = { ...review, booking_id: "BK-TEST", status: "confirmed" };
const secondBooking = { ...booking, booking_id: "BK-TEST-2", date: bookingDate.slice(0, -2) + "17" };
const cancellation = { ...review, type: "cancel_booking", action_id: "action-b" };
const arabicOptions = "1. 2:00 مساءً مع د. سامي نصار\n٢. ١:١٥ مساءً مع د. رانيا خوري";
const englishOptions = "1. 2:00 PM with Dr. Sami Nassar\n2. 1:15 PM with Dr. Rania Khoury";

let slowCompleted = false;
let failNextSTT = false;

async function serve(request, response) {
    const { pathname } = new URL(request.url, "http://localhost");
    const json = (value, status = 200) => {
        response.writeHead(status, { "Content-Type": "application/json" });
        response.end(JSON.stringify(value));
    };
    if (pathname.startsWith("/api/speech/")) {
        const token = pathname.split("/").at(-1);
        requests.push({ pathname, params: {} });
        if (token === "failure" && requestsTo(pathname).length === 1) return json({ detail: "Speech unavailable" }, 503);
        response.writeHead(200, { "Content-Type": "audio/pcm", "X-Audio-Sample-Rate": "24000" });
        response.write(Buffer.alloc(token === "slow" ? 1921 : 4801, 1));
        await delay(token === "slow" ? 3000 : 120);
        if (!response.destroyed) {
            if (token === "slow") slowCompleted = true;
            response.end(Buffer.alloc(token === "broken" ? 4800 : 4799, 1));
        }
        return;
    }
    if (pathname === "/health") return json({ status: "ok" });
    if (pathname.startsWith("/api/")) {
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        const body = Buffer.concat(chunks);
        const contentType = request.headers["content-type"] || "";
        const params = contentType.startsWith("multipart/form-data")
            ? Object.fromEntries(await new Request("http://localhost", {
                method: "POST", body, headers: { "Content-Type": contentType },
            }).formData())
            : body.length ? JSON.parse(body.toString("utf8")) : {};
        requests.push({ pathname, params });
        if (pathname === "/api/chat") {
            // Leave a window where a second click or Enter could duplicate the turn.
            await delay(300);
            if (params.message.startsWith("speech-")) {
                const token = params.message.slice("speech-".length);
                const language = token === "typed-ar" ? "ar" : "en";
                return json({ reply: language === "ar" ? arabicOptions : englishOptions, language, speech_token: token });
            }
            if (params.message === "أسنان") return json({ reply: arabicOptions, language: "ar" });
            if (params.message === "Cancel this appointment") {
                return json({ reply: "Review your cancellation.", language: "en", appointment_review: cancellation });
            }
            return json({ reply: "Review your visit.", language: "en", appointment_review: review });
        }
        if (pathname === "/api/voice") {
            if (failNextSTT) {
                failNextSTT = false;
                return json({ category: "stt_provider_error", transcript: "",
                    reply: "تعذّر التعرّف على الصوت مؤقتًا. ممكن إعادة التسجيل أو كتابة الرسالة.",
                    speech_token: "voice-error" }, 503);
            }
            if (requestsTo("/api/voice").length > 3) {
                const ar = params.language_hint === "ar";
                return json({ transcript: ar ? "موعد أسنان" : "Dental appointment",
                    reply: ar ? arabicOptions : englishOptions, language: params.language_hint,
                    speech_token: "voice-" + params.language_hint });
            }
            if (requestsTo("/api/voice").length === 2) {
                return json({ transcript: "Unverified partial transcript" }, 422);
            }
            if (params.language_hint === "en") {
                return json({ transcript: "Dental appointment", reply: englishOptions, language: "en",
                    appointment_review: review });
            }
            return json({
                transcript: "موعد أسنان", reply: arabicOptions, language: "ar",
                appointment_review: review,
            });
        }
        if (pathname === "/api/approve") {
            assert.equal(params.approved, true);
            if (params.action_id === cancellation.action_id) {
                return json({
                    reply: "Your visit is cancelled.", language: "en",
                    appointment: { booking_id: booking.booking_id, status: "cancelled" },
                    session_appointments: [{ ...booking, status: "cancelled" }, secondBooking],
                });
            }
            assert.equal(params.action_id, review.action_id);
            return json({ reply: "Your visit is confirmed.", language: "en", appointment: booking,
                session_appointments: [booking, secondBooking] });
        }
        if (pathname === "/api/voice/discard") return json({ status: "ok" });
        return json({ error: "Unhandled test route" }, 404);
    }
    const asset = pathname === "/" ? "index.html" : pathname.match(/^\/clinic-assets\/([\w.-]+)$/)?.[1];
    if (asset && existsSync(join(web, asset))) {
        const extension = asset.slice(asset.lastIndexOf("."));
        response.writeHead(200, { "Content-Type": contentTypes[extension] || "application/octet-stream" });
        response.end(readFileSync(join(web, asset)));
    } else {
        response.writeHead(404).end();
    }
}

const server = createServer((req, res) => {
    serve(req, res).catch(error => {
        console.error(error);
        res.writeHead(500).end();
    });
});
await new Promise((resolve, reject) => server.listen(0, "127.0.0.1", resolve).once("error", reject));
const origin = `http://127.0.0.1:${server.address().port}`;
const tempRoot = realpathSync(tmpdir());
const profile = mkdtempSync(join(tempRoot, "zaytoun-browser-"));
const browser = spawn(executable, [
    "--headless", "--disable-gpu", "--disable-background-networking", "--no-first-run",
    "--disable-extensions", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "about:blank",
], { stdio: ["ignore", "ignore", "pipe"], windowsHide: true });
let browserOutput = "";
browser.stderr.setEncoding("utf8");
browser.stderr.on("data", data => { browserOutput += data; });
let browserError;
browser.on("error", error => { browserError = error; });
const sockets = [];

async function until(check, description, timeout = 7000) {
    const limit = Date.now() + timeout;
    while (Date.now() < limit) {
        const value = await check();
        if (value) return value;
        await delay(30);
    }
    throw new Error(`Timed out waiting for ${description}`);
}

function mediaFixture() {
    window.testSpeech = [];
    window.testPCM = { starts: 0, stops: 0, overlap: false, failNextStart: false };
    const scheduled = new Map();
    const createSource = AudioContext.prototype.createBufferSource;
    AudioContext.prototype.createBufferSource = function () {
        const source = createSource.call(this);
        const start = source.start.bind(source);
        const stop = source.stop.bind(source);
        source.start = at => {
            if (testPCM.failNextStart) {
                testPCM.failNextStart = false;
                throw new Error("Test playback failure");
            }
            const end = at + source.buffer.duration;
            for (const [otherStart, otherEnd] of scheduled.values()) {
                if (at < otherEnd - 0.00001 && end > otherStart + 0.00001) testPCM.overlap = true;
            }
            start(at);
            scheduled.set(source, [at, end]);
            testPCM.starts++;
        };
        source.stop = (...args) => {
            scheduled.delete(source);
            testPCM.stops++;
            return stop(...args);
        };
        source.addEventListener("ended", () => scheduled.delete(source), { once: true });
        return source;
    };
    speechSynthesis.speak = utterance => {
        testSpeech.push({ text: utterance.text, lang: utterance.lang });
    };
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    window.testMic = { requests: 0, streams: [], deny: false };
    navigator.mediaDevices.getUserMedia = async options => {
        window.testMic.requests += 1;
        if (window.testMic.deny) throw new DOMException("Test denied microphone", "NotAllowedError");
        const stream = await original(options);
        window.testMic.streams.push(stream);
        return stream;
    };
}

async function openPage(pageUrl, debuggingPort) {
    const target = await fetch(`http://127.0.0.1:${debuggingPort}/json/new?about:blank`, { method: "PUT" });
    assert.equal(target.status, 200);
    const socket = new WebSocket((await target.json()).webSocketDebuggerUrl);
    sockets.push(socket);
    await new Promise((resolve, reject) => {
        socket.addEventListener("open", resolve, { once: true });
        socket.addEventListener("error", reject, { once: true });
    });
    const pending = new Map();
    let id = 0;
    socket.addEventListener("message", event => {
        const data = JSON.parse(event.data);
        const callbacks = pending.get(data.id);
        if (!callbacks) return;
        pending.delete(data.id);
        if (data.error) callbacks.reject(new Error(data.error.message));
        else callbacks.resolve(data.result);
    });
    socket.addEventListener("close", () => {
        for (const { reject } of pending.values()) reject(new Error("Browser connection closed"));
    });
    function command(method, params) {
        return new Promise((resolve, reject) => {
            pending.set(++id, { resolve, reject });
            socket.send(JSON.stringify({ id, method, params }));
        });
    }
    const evaluate = async expression => {
        const result = await command("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
        if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text);
        return result.result.value;
    };
    await command("Page.navigate", { url: pageUrl });
    const wait = (expression, description = expression) => until(() => evaluate(expression), description);
    await wait(`location.origin === ${JSON.stringify(pageUrl)} && document.readyState === "complete"`, "clinic page");
    await evaluate(`(${mediaFixture.toString()})()`);
    const click = async selector => {
        const lookup = `document.querySelector(${JSON.stringify(selector)})`;
        await wait(`Boolean(${lookup}?.getClientRects().length && !${lookup}.disabled)`);
        // Trusted pointer events supply user activation for microphone/Web Audio.
        const point = await evaluate(`(() => {
            const button = ${lookup};
            button.scrollIntoView({block: "nearest"});
            const {left, top, width, height} = button.getBoundingClientRect();
            return {x: left + width / 2, y: top + height / 2};
        })()`);
        await command("Input.dispatchMouseEvent", { type: "mousePressed", ...point, button: "left", clickCount: 1 });
        await command("Input.dispatchMouseEvent", { type: "mouseReleased", ...point, button: "left", clickCount: 1 });
    };
    return { evaluate, click, wait, command };
}

const requestsTo = endpoint => requests.filter(item => item.pathname === endpoint).map(item => item.params);

try {
    const port = await until(() => {
        if (browserError || browser.exitCode !== null) {
            throw new Error(`Chromium failed to launch: ${browserError ?? browserOutput}`);
        }
        return browserOutput.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/)?.[1];
    }, "Chromium debugging port", 15000);
    const page = await openPage(origin, port);
    const { evaluate, click, wait } = page;
    await click("#choose-en");
    assert.equal(await evaluate('document.querySelector("#workspace").hidden'), false);
    assert.equal(await evaluate('document.querySelector("#welcome-continue, #address-preference")'), null);
    await wait('document.documentElement.lang === "en" && document.documentElement.dir === "ltr"');

    await evaluate(`(() => {
        const input = document.querySelector("#message");
        input.value = "Dental checkup";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.focus();
    })()`);
    const enter = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 };
    await page.command("Input.dispatchKeyEvent", { type: "keyDown", ...enter, text: "\r", modifiers: 8 });
    await page.command("Input.dispatchKeyEvent", { type: "keyUp", ...enter, modifiers: 8 });
    await wait('document.querySelector("#message").value.includes(String.fromCharCode(10))', "Shift+Enter inserts a newline");
    assert.equal(requestsTo("/api/chat").length, 0);
    await page.command("Input.dispatchKeyEvent", { type: "keyDown", ...enter });
    await page.command("Input.dispatchKeyEvent", { type: "keyUp", ...enter });
    await evaluate('document.querySelector("#send").click()');
    await wait('document.querySelector("#messages").textContent.includes("Review your visit.")');
    assert.equal(await evaluate('Boolean(document.querySelector("#diagnostics"))'), false);
    assert.equal(requestsTo("/api/chat").length, 1, "Enter and send must not duplicate a pending turn");
    assert.deepEqual(await evaluate("testSpeech"), [], "Private review stays silent");
    const [chat] = requestsTo("/api/chat");
    assert.equal(chat.message, "Dental checkup");
    await wait('document.querySelector("#appointments").textContent.includes("Lina Mansour")');
    assert.equal(requestsTo("/api/approve").length, 0);

    // Switching language keeps the same review and the same session.
    await click("#language");
    await wait('document.documentElement.lang === "ar" && document.documentElement.dir === "rtl"');
    await wait('document.querySelector("#appointments").textContent.includes("فحص الأسنان")');
    await wait('Boolean(document.querySelector(".appt-month span")?.textContent.match(/[\\u0600-\\u06ff]/))');
    assert.equal(requestsTo("/api/approve").length, 0);

    // Stop processes fake microphone data with the selected language.
    await click("#mic");
    await wait('testMic.requests === 1 && Boolean(document.querySelector("#cancel-recording")?.getClientRects().length)');
    await delay(300);
    await click("#mic");
    await wait('document.querySelector("#messages").textContent.includes("موعد أسنان")');
    assert.ok((await evaluate('document.querySelector("#messages").textContent')).includes(arabicOptions));
    assert.equal(await evaluate("testSpeech.length"), 0);
    const [voice] = requestsTo("/api/voice");
    assert.equal(requestsTo("/api/voice").length, 1);
    assert.equal(voice.session_id, chat.session_id);
    assert.equal(voice.language_hint, "ar");
    assert.ok(voice.audio.size > 44);
    const wav = Buffer.from(await voice.audio.arrayBuffer());
    assert.equal(wav.toString("ascii", 0, 4), "RIFF");
    assert.equal(wav.toString("ascii", 8, 12), "WAVE");
    assert.equal(wav.readUInt16LE(22), 1);
    assert.equal(requestsTo("/api/approve").length, 0);

    // Cancel releases capture, leaves chat untouched and never sends the audio.
    const beforeCancel = await evaluate('document.querySelector("#messages").textContent');
    await click("#mic");
    await wait('testMic.requests === 2 && Boolean(document.querySelector("#cancel-recording")?.getClientRects().length)');
    await click("#cancel-recording");
    await wait('testMic.streams[1].getTracks().every(track => track.readyState === "ended")');
    await delay(200);
    assert.equal(requestsTo("/api/voice").length, 1);
    assert.equal(requestsTo("/api/chat").length, 1);
    assert.equal(await evaluate('document.querySelector("#messages").textContent'), beforeCancel);

    await evaluate('testMic.deny = true');
    await click("#mic");
    await wait('Boolean(document.querySelector("#error")?.textContent.trim())', "localized microphone error");
    assert.match(await evaluate('document.querySelector("#error").textContent'), /[\u0600-\u06ff]/);
    assert.equal(requestsTo("/api/voice").length, 1);
    assert.equal(await evaluate('document.querySelector("#messages").textContent'), beforeCancel);

    await evaluate('testMic.deny = false');
    await click("#mic");
    await wait('testMic.requests === 4 && Boolean(document.querySelector("#cancel-recording")?.getClientRects().length)');
    await delay(300);
    await click("#mic");
    await wait('document.querySelector("#error").textContent.includes("تفريغ")', "localized transcription error");
    assert.equal(requestsTo("/api/voice").length, 2);
    assert.equal(await evaluate("testSpeech.length"), 0, "Transcription errors are silent");
    assert.equal(await evaluate('document.querySelector("#messages").textContent'), beforeCancel);

    await click("#language");
    await wait('document.documentElement.lang === "en"');
    await click("#mic");
    await wait('Boolean(document.querySelector("#cancel-recording")?.getClientRects().length)');
    await delay(300);
    await click("#mic");
    await wait(`document.querySelector("#messages").textContent.includes(${JSON.stringify(englishOptions)})`);
    await wait('document.querySelector("#loading").hidden');
    assert.equal(await evaluate('document.querySelectorAll(".assistant .replay").length'), 0);
    await click("#language");
    await wait('document.documentElement.lang === "ar"');

    const day = `.appt-day[data-date="${bookingDate}"]`;
    await click(day);
    await wait(`document.querySelector('${day}').getAttribute("aria-pressed") === "true"`);
    assert.equal(requestsTo("/api/approve").length, 0, "Calendar selection cannot confirm the review");
    await click(".appt-approve");
    await wait('document.querySelector("#messages").textContent.includes("Your visit is confirmed.")');
    assert.deepEqual(requestsTo("/api/approve"), [{ session_id: chat.session_id, action_id: review.action_id, approved: true }]);
    await click(day);
    await wait(`Boolean(document.querySelector('${day} .appt-marker'))`);
    await wait('document.querySelector(".appt-record").textContent.includes("فحص الأسنان")');
    assert.equal(await evaluate('document.querySelectorAll(".appt-record").length'), 1);
    const otherDay = bookingDate.slice(0, -2) + "16";
    await click(`.appt-day[data-date="${otherDay}"]`);
    await wait('Boolean(document.querySelector(".appt-empty")) && !document.querySelector(".appt-record")');
    assert.notEqual(await evaluate('document.querySelector(".appt-filter").textContent'), "كل المواعيد في هذه المحادثة");
    await click(".appt-all");
    await wait('document.querySelector(".appt-filter").textContent === "كل المواعيد في هذه المحادثة" && document.querySelectorAll(".appt-record").length === 2');
    assert.equal(requestsTo("/api/approve").length, 1);

    await evaluate(`(() => {
        const input = document.querySelector("#message");
        input.value = "Cancel this appointment";
        input.dispatchEvent(new Event("input", { bubbles: true }));
    })()`);
    await click("#send");
    await wait('document.querySelector("#messages").textContent.includes("Review your cancellation.")');
    assert.equal(requestsTo("/api/approve").length, 1, "Preparing cancellation cannot cancel the booking");
    await click(".appt-approve");
    await wait('Boolean(document.querySelector(".appt-record .appt-status[data-status=cancelled]"))');
    assert.equal(await evaluate(`Boolean(document.querySelector('${day} .appt-marker'))`), false);
    assert.equal(await evaluate(`Boolean(document.querySelector('[data-date="${secondBooking.date}"] .appt-marker'))`), true);
    assert.deepEqual(requestsTo("/api/approve").at(-1), {
        session_id: chat.session_id, action_id: cancellation.action_id, approved: true,
    });

    const second = await openPage(origin, port);
    assert.equal(await second.evaluate("testSpeech.length"), 0);
    await second.wait('document.documentElement.dir === "rtl"', "persisted Arabic preference");
    await second.wait('Boolean(document.querySelector("#appointments")?.textContent.trim())');
    assert.doesNotMatch(await second.evaluate('document.querySelector("#appointments").textContent'), /BK-TEST|Lina Mansour|Dr\. Rania|د\. رانيا/);
    const typeSpeech = async token => {
        await second.evaluate(`(() => {
            const input = document.querySelector("#message");
            input.value = "speech-" + ${JSON.stringify(token)};
            input.dispatchEvent(new Event("input", { bubbles: true }));
        })()`);
        await second.click("#send");
        await second.wait('document.querySelector("#loading").hidden');
    };
    await typeSpeech("typed-ar");
    await second.wait("testPCM.starts === 2");
    assert.ok((await second.evaluate('document.querySelector("#messages").textContent')).includes(arabicOptions));
    assert.equal(await second.evaluate("testSpeech.length"), 0);
    await second.click(".assistant:last-child .replay");
    await second.wait("testPCM.starts === 3");
    assert.equal(requestsTo("/api/speech/typed-ar").length, 1, "Replay uses cached PCM");

    await second.click("#spoken-replies");
    await typeSpeech("typed-en");
    assert.equal(requestsTo("/api/speech/typed-en").length, 0, "Mute avoids the upstream request");
    await second.click("#spoken-replies");
    await second.click(".assistant:last-child .replay");
    await second.wait("testPCM.starts === 5");
    assert.equal(await second.evaluate("testSpeech.length"), 0);

    for (const language of ["ar", "en"]) {
        if (language === "en") await second.click("#language");
        const before = await second.evaluate("testPCM.starts");
        await second.click("#mic");
        await second.wait('!document.querySelector("#cancel-recording").hidden');
        await delay(300);
        await second.click("#mic");
        await second.wait(`testPCM.starts === ${before + 2}`);
        assert.equal(requestsTo("/api/speech/voice-" + language).length, 1);
        assert.equal(await second.evaluate("testSpeech.length"), 0);
    }

    const beforeSlow = await second.evaluate("testPCM.starts");
    await typeSpeech("slow");
    await second.wait(`testPCM.starts === ${beforeSlow + 1}`, "first chunk plays before the slow stream ends");
    assert.equal(slowCompleted, false, "A 40 ms PCM chunk starts playback before the response finishes");
    const stopped = await second.evaluate("testPCM.stops");
    await typeSpeech("failure");
    assert.ok(await second.evaluate("testPCM.stops") > stopped, "A new message stops scheduled audio");
    await second.wait('!document.querySelector("#speech-status").hidden', "provider failure offers retry");
    assert.equal(await second.evaluate("testSpeech.length"), 0);
    assert.equal(requestsTo("/api/speech/failure").length, 1);
    assert.equal(await second.evaluate("testPCM.starts"), beforeSlow + 1);
    await second.click(".assistant:last-child .replay");
    await second.wait(`testPCM.starts === ${beforeSlow + 3}`, "retry streams Gemini audio");
    assert.equal(requestsTo("/api/speech/failure").length, 2);
    await typeSpeech("broken");
    await second.wait('!document.querySelector("#speech-status").hidden', "truncated PCM shows replay prompt");
    assert.equal(await second.evaluate("testSpeech.length"), 0);
    await second.click(".assistant:last-child .replay");
    await second.wait('!document.querySelector("#speech-status").hidden');
    assert.equal(requestsTo("/api/speech/broken").length, 2);
    assert.equal(await second.evaluate("testSpeech.length"), 0);
    await second.evaluate("testPCM.failNextStart = true");
    const beforeBlocked = await second.evaluate("testPCM.starts");
    await typeSpeech("playback-blocked");
    await second.wait('!document.querySelector("#speech-status").hidden', "audio start failure offers retry");
    assert.equal(await second.evaluate("testPCM.starts"), beforeBlocked);
    await second.click(".assistant:last-child .replay");
    await second.wait(`testPCM.starts === ${beforeBlocked + 2}`);
    assert.equal(requestsTo("/api/speech/playback-blocked").length, 2);
    assert.equal(await second.evaluate("testPCM.overlap"), false, "PCM buffers never overlap");
    assert.equal(await second.evaluate("testSpeech.length"), 0, "Replay never invokes browser synthesis");
    await second.click("#language");

    await second.evaluate('localStorage.removeItem("zaytoun.language")');
    const third = await openPage(origin, port);
    await third.command("Emulation.setDeviceMetricsOverride", {
        width: 360, height: 780, deviceScaleFactor: 1, mobile: true,
    });
    await third.click("#choose-ar");
    await third.wait('document.documentElement.lang === "ar" && document.documentElement.dir === "rtl"');
    assert.equal(await third.evaluate("document.documentElement.scrollWidth"), 360, "Mobile RTL page fits its viewport");

    failNextSTT = true;
    await third.click("#mic");
    await third.wait('!document.querySelector("#cancel-recording").hidden');
    await delay(300);
    await third.click("#mic");
    await third.wait('document.querySelector("#error").textContent.includes("إعادة التسجيل")');
    await third.wait('testPCM.starts > 0');
    assert.equal(await third.evaluate('document.querySelectorAll("#messages .assistant .replay").length'), 1);
    assert.equal(requestsTo("/api/speech/voice-error").length, 1);
    assert.equal(await third.evaluate('document.querySelector("#mic").disabled || document.querySelector("#message").hidden'), false);
    await third.click("#mic");
    await third.wait('!document.querySelector("#cancel-recording").hidden');
    await delay(300);
    await third.click("#mic");
    await third.wait('document.querySelector("#messages").textContent.includes("موعد أسنان")');

    assert.equal(await third.evaluate(`(async () => {
        const { createSpeechStream } = await import("/clinic-assets/speech.js");
        const NativeContext = window.AudioContext;
        const nativeFetch = window.fetch;
        let finishResume;
        let requested = false;
        window.AudioContext = class {
            state = "suspended";
            resume() { return new Promise(resolve => { finishResume = resolve; }); }
        };
        window.fetch = () => {
            requested = true;
            return Promise.resolve(new Response(null, { status: 404 }));
        };
        const player = createSpeechStream();
        try {
            player.unlock();
            const playback = player.play({ speech_token: "fixture" });
            const requestedBeforeResume = requested;
            finishResume();
            await playback;
            return requestedBeforeResume;
        } finally {
            window.AudioContext = NativeContext;
            window.fetch = nativeFetch;
        }
    })()`), true, "The speech request starts while AudioContext is resuming");

    console.log("Clinic browser checks passed (Edge/Chromium fake microphone; mocked HTTP).");
} finally {
    for (const socket of sockets) socket.close();
    server.close();
    if (browser.pid && browser.exitCode === null) {
        browser.kill();
        await Promise.race([new Promise(resolve => browser.once("exit", resolve)), delay(2000)]);
    }
    // Chromium can briefly keep files open on Windows after the main process exits.
    try {
        const resolvedProfile = realpathSync(profile);
        assert.equal(dirname(resolvedProfile), tempRoot);
        assert.equal(resolvedProfile, profile);
        rmSync(resolvedProfile, { recursive: true, force: true, maxRetries: 4, retryDelay: 150 });
    } catch (error) {
        console.warn(`Temporary browser profile could not be removed: ${error.message}`);
    }
}
