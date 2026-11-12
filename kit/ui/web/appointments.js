const labels = {
    en: {
        calendar: "Calendar", appointments: "My Appointments", review: "Review appointment",
        cancelReview: "Review cancellation", previous: "Previous month", next: "Next month",
        all: "Show all", allFilter: "All appointments in this conversation", empty: "Appointments booked in this conversation will appear here.",
        emptyDay: "No appointments on this date.", service: "Service", date: "Date",
        time: "Time", doctor: "Doctor", patient: "Patient", phone: "Phone",
        status: "Status", confirmed: "Confirmed", cancelled: "Cancelled",
        pending: "Awaiting approval", available: "Available", approve: "Confirm booking",
        approveCancellation: "Confirm cancellation", reject: "Decline", unknown: "Unknown",
    },
    ar: {
        calendar: "التقويم", appointments: "مواعيدي", review: "مراجعة الموعد",
        cancelReview: "مراجعة الإلغاء", previous: "الشهر السابق", next: "الشهر التالي",
        all: "عرض الكل", allFilter: "كل المواعيد في هذه المحادثة", empty: "ستظهر هنا المواعيد المحجوزة في هذه المحادثة.",
        emptyDay: "لا توجد مواعيد في هذا اليوم.", service: "الخدمة", date: "التاريخ",
        time: "الوقت", doctor: "الطبيب", patient: "المريض", phone: "الهاتف",
        status: "الحالة", confirmed: "مؤكد", cancelled: "ملغى",
        pending: "بانتظار الموافقة", available: "متاح", approve: "تأكيد الحجز",
        approveCancellation: "تأكيد الإلغاء", reject: "رفض", unknown: "غير معروف",
    },
};

const calendarState = new WeakMap();

function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content) node.textContent = content;
    return node;
}

function button(className, text, handler) {
    const node = element("button", className, text);
    node.type = "button";
    node.addEventListener("click", handler);
    return node;
}

function isoDay(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return year + "-" + month + "-" + day;
}

function dayText(date, locale) {
    return new Intl.DateTimeFormat(locale, { dateStyle: "full" }).format(date);
}

function timeText(time, locale) {
    const match = /^(\d{2}):(\d{2})/.exec(time || "");
    return match ? new Intl.DateTimeFormat(locale, {
        hour: "numeric", minute: "2-digit",
    }).format(new Date(2026, 0, 1, Number(match[1]), Number(match[2]))) : "";
}

function bookingName(record, key, language) {
    const english = record[key + "_name"];
    const arabic = record[key + "_name_ar"];
    return language === "ar" ? arabic || english : english || arabic;
}

function renderReview(pending, busy, onDecision, language, locale, t) {
    const cancellation = pending.type === "cancel_booking";
    const review = element("section", "appt-review");
    review.append(element("h2", "", cancellation ? t.cancelReview : t.review));
    const fields = element("dl", "appt-fields");
    const details = [
        [t.service, bookingName(pending, "service", language)],
        [t.date, pending.date && dayText(new Date(pending.date + "T12:00:00"), locale)],
        [t.time, timeText(pending.start, locale)],
        [t.doctor, bookingName(pending, "provider", language)],
        [t.patient, pending.patient_name],
        [t.phone, pending.masked_phone],
        [t.status, t.pending],
    ];
    for (const [label, value] of details) {
        if (!value) continue;
        const field = element("dd", "", value);
        field.dir = "auto";
        fields.append(element("dt", "", label), field);
    }
    const actions = element("div", "appt-actions");
    const approve = button("appt-approve", cancellation ? t.approveCancellation : t.approve,
        () => onDecision(pending.action_id, true));
    const reject = button("appt-reject", t.reject, () => onDecision(pending.action_id, false));
    approve.disabled = reject.disabled = busy;
    actions.append(approve, reject);
    review.append(fields, actions);
    return review;
}

export function renderAppointments({root, language, records, pending, busy, onDecision}) {
    const t = labels[language] || labels.en;
    const locale = language === "ar" ? "ar-PS-u-ca-gregory" : "en-GB";
    const today = new Date();
    if (!calendarState.has(root)) {
        calendarState.set(root, {
            month: new Date(today.getFullYear(), today.getMonth(), 1),
            selected: null,
        });
    }
    const state = calendarState.get(root);
    const confirmed = new Set(records.filter(record => record.status === "confirmed").map(record => record.date));
    const calendar = element("section", "appt-calendar");
    const list = element("section", "appt-list");
    list.setAttribute("aria-live", "polite");

    function drawList() {
        const title = element("h2", "", t.appointments);
        const filter = state.selected ? dayText(new Date(state.selected + "T12:00:00"), locale) : t.allFilter;
        list.replaceChildren(title, element("p", "appt-filter", filter));
        if (state.selected) {
            list.append(button("appt-all", t.all, () => {
                state.selected = null;
                drawCalendar();
                drawList();
                titleFocus(list.querySelector("h2"));
            }));
        }
        const shown = records.filter(record => !state.selected || record.date === state.selected);
        if (!shown.length) {
            list.append(element("p", "appt-empty", state.selected ? t.emptyDay : t.empty));
        }
        for (const record of shown) {
            const card = element("article", "appt-record");
            const status = element("span", "appt-status", t[record.status] || t.unknown);
            status.dataset.status = record.status;
            const title = element("h3", "", bookingName(record, "service", language) || t.appointments);
            const when = [
                record.date && dayText(new Date(record.date + "T12:00:00"), locale),
                timeText(record.start, locale),
            ].filter(Boolean).join(" · ");
            card.append(title, status);
            if (when) card.append(element("p", "", when));
            const doctor = bookingName(record, "provider", language);
            if (doctor) card.append(element("p", "", doctor));
            list.append(card);
        }
    }

    function titleFocus(title) {
        title.tabIndex = -1;
        title.focus();
    }

    function drawCalendar() {
        const title = element("h2", "", t.calendar);
        const nav = element("div", "appt-month");
        const monthName = element("span", "", new Intl.DateTimeFormat(locale, {
            month: "long", year: "numeric",
        }).format(state.month));
        const year = state.month.getFullYear();
        const month = state.month.getMonth();
        function monthButton(move, caption, direction) {
            const control = button("appt-nav", direction, () => {
                state.month = new Date(year, month + move, 1);
                drawCalendar();
                calendar.querySelector('[data-move="' + move + '"]').focus();
            });
            control.dataset.move = move;
            control.setAttribute("aria-label", caption);
            return control;
        }
        const rtl = language === "ar";
        nav.append(monthButton(-1, t.previous, rtl ? "›" : "‹"), monthName,
            monthButton(1, t.next, rtl ? "‹" : "›"));

        const grid = element("div", "appt-grid");
        const weekday = new Intl.DateTimeFormat(locale, { weekday: "short" });
        const number = new Intl.NumberFormat(locale);
        for (let i = 0; i < 7; i++) {
            grid.append(element("span", "appt-weekday", weekday.format(new Date(2026, 0, 4 + i))));
        }
        for (let i = 0; i < state.month.getDay(); i++) grid.append(element("span"));
        for (let day = 1; day <= new Date(year, month + 1, 0).getDate(); day++) {
            const date = new Date(year, month, day);
            const key = isoDay(date);
            const dayButton = button("appt-day", number.format(day), () => {
                state.selected = key;
                drawCalendar();
                drawList();
                calendar.querySelector('[data-date="' + key + '"]').focus();
            });
            dayButton.dataset.date = key;
            dayButton.setAttribute("aria-pressed", String(state.selected === key));
            if (key === isoDay(today)) dayButton.setAttribute("aria-current", "date");
            const booked = confirmed.has(key);
            dayButton.setAttribute("aria-label", dayText(date, locale) + (booked ? " · " + t.confirmed : ""));
            if (booked) {
                const marker = element("span", "appt-marker");
                marker.setAttribute("aria-hidden", "true");
                dayButton.append(marker);
            }
            grid.append(dayButton);
        }
        calendar.replaceChildren(title, nav, grid);
    }

    drawCalendar();
    drawList();
    root.replaceChildren(calendar, list);
    if (pending) root.prepend(renderReview(pending, busy, onDecision, language, locale, t));
}
