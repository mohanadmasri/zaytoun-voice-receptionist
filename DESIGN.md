# Zaytoun Voice Receptionist — System Design

## 1. Project Goal

Build an AI-powered voice receptionist that helps patients
book clinic appointments in English and Arabic.

The system must check real available slots through MCP
and must never create a booking without explicit patient approval.


<!--
المريض يرسل طلبه صوتيًا أو كتابيًا عبر Gradio UI.
يستقبل FastAPI الطلب ويمرره إلى LangGraph الذي ينظم سير العمل.
الـIntake يفهم الطلب، والـScheduler يبحث عن المواعيد عبر MCP.
الـMCP Gateway يتواصل مع نظام العيادة للحصول على بيانات حقيقية.
عندما يختار المريض موعدًا، نعرض تفاصيله وننتظر موافقته.
فقط Approval Handler ينفذ الحجز بعد الموافقة، وليس الـAI مباشرة.
-->

## 2. System Architecture

The system consists of the following components:

1. Gradio UI: Receives patient text or voice requests.
2. FastAPI Backend: Handles incoming requests and responses.
3. LangGraph: Coordinates the conversation and agent workflow.
4. Intake Agent: Understands the request and collects missing details.
5. Scheduler Agent: Searches for available appointments through MCP.
6. MCP Gateway: Communicates with the clinic's MCP server.
7. Clinic API: Manages actual appointment availability and bookings.
8. Approval Handler: Executes bookings only after explicit approval.

The system uses session memory to maintain conversation context.


<!--
قسمنا النظام إلى Agentين متخصصين لكل منهما مسؤولية مختلفة.

Intake Agent:
يستخدم LLM لفهم كلام المريض واستخراج الخدمة والتاريخ والوقت والاسم والهاتف.
يسأل سؤالًا واحدًا عند نقص المعلومات، ويحفظ ما جمعه في الـState.
التواريخ النسبية يحسبها Python اعتمادًا على تاريخ العيادة.

Scheduler Agent:
يأخذ المعلومات من Intake ويبحث عن مواعيد حقيقية باستخدام MCP.
يقترح فقط المواعيد التي أعادتها أداة find_slots.
إذا كان الموعد محجوزًا، يبحث عن بدائل.

LangGraph ينظم الانتقال بينهما حسب حالة المحادثة.
ولا يملك أي منهما صلاحية إنشاء الحجز مباشرة.
-->
## 3. Agent Roles

### Intake Agent — Developer A

The Intake Agent uses an LLM to understand patient requests
in English and Arabic.

It extracts the requested service, date, time preference,
patient name, and phone number.

It updates the conversation state and asks one focused
question when required information is missing.

Relative dates are resolved by Python code using the
clinic's current date.

The Intake Agent cannot create or cancel bookings.

### Scheduler Agent — Developer B

The Scheduler Agent uses the collected requirements to
search for real appointment slots through the MCP gateway.

It recommends only slots returned by the clinic's find_slots tool.

If the requested time is unavailable, it offers real alternatives.

The Scheduler Agent cannot create or cancel bookings.

### Agent Coordination

LangGraph coordinates both agents using shared conversation
state and conditional transitions.

When enough appointment information is available, the workflow
can move from Intake to Scheduler.

The booking is executed only by the approval handler
after explicit patient confirmation.





<!--
الـState تحفظ معلومات المريض خلال المحادثة، مثل اللغة والخدمة والتاريخ والاسم والهاتف.
كل محادثة لها session_id خاص بها، مما يسمح باسترجاع معلوماتها بين الرسائل.
إذا كانت معلومة ضرورية ناقصة (None)، يستخدم LangGraph انتقالًا مشروطًا لطلب توضيح بدل التخمين.
الـIntake يجمع المعلومات، والـScheduler يبحث عن المواعيد الحقيقية.
ندعم العربية والإنجليزية فقط، ونرفض اللغات الأخرى عند اكتشافها بوضوح.
لا يتم إنشاء حجز دون موافقة المريض.
-->

## 4. Conversation State and Language Policy

### Conversation State

LangGraph uses shared state to track information collected
from the patient and control the conversation workflow.

Each conversation is identified by a unique session_id.
The application preserves the session state between messages.

The state includes:

- language: Detected language (en, ar, unknown, or unsupported).
- service_id: Requested clinic service.
- requested_date: Resolved appointment date.
- time_window: Patient's preferred time range, if provided.
- patient_name: Patient's name, if known.
- patient_phone: Patient's phone number, if known.
- proposed_slots: Available slots returned by the clinic.
- selected_slot_id: Slot selected by the patient.
- pending_action: Booking awaiting explicit approval.
- stage: Current conversation workflow stage.

Missing information is represented as None.

The Intake Agent updates the state after each message.
If required information is missing, the system asks
one clarification question at a time.

The Scheduler Agent receives the collected requirements
and searches for real available appointments.

Patient identity must be collected before creating
a pending booking action.

### Language Policy

The system supports English and Arabic only.

Incoming text is checked for language before booking
details are processed.

If a clearly unsupported language is detected,
the system politely asks the patient to continue
in English or Arabic.

Short or ambiguous messages are handled using
the conversation context instead of being rejected
without sufficient evidence.

Unsupported-language requests must not trigger
appointment searches or booking actions.


<!--
— Memory:

Short-Term Memory:
نحفظ معلومات الحجز وسياق المحادثة في SessionStore باستخدام session_id،
حتى يتذكر النظام إجابات المريض بين الرسائل.

Long-Term Memory:
نستخدم get_patient عبر MCP لاسترجاع بيانات المريض وتفضيلاته
برقم هاتفه، حتى لو بدأ محادثة جديدة.

لا نحتاج Vector Database لأن البحث برقم الهاتف مباشر ولا يتطلب بحثًا دلاليًا.
-->
## 5. Persistent Memory

The system uses two types of memory.

### Short-Term Memory

Conversation state is stored in a SessionStore,
identified by session_id.

This allows the system to remember collected information
across multiple messages in the same conversation.

### Long-Term Patient Memory

The system uses the clinic's patient records through MCP.

When a patient provides a phone number, the system calls
get_patient to retrieve existing information and preferences.

Known preferences may help personalize appointment suggestions,
but the patient's current request always takes priority.

After a successful booking, the system may update relevant
patient information through the clinic's supported tools.

Only necessary patient information is stored.
Medical diagnoses and sensitive medical details are excluded.

The demo clinic stores its data in memory, so patient records
are not guaranteed to survive a clinic service restart.



<!--
 Safety Guardrails:

Input: التحقق من المدخلات واللغة ومنع محاولات التلاعب بالتعليمات.
Medical: عدم تقديم تشخيص أو أدوية، وتوجيه الطوارئ إلى 101.
Action: الـLLM لا ينفذ الحجز مباشرة؛ يلزم Approval Handler وموافقة صريحة.
Output: لا نقترح إلا مواعيد find_slots ولا نؤكد الحجز دون نتيجة فعلية.
Resource Limits: تقييد استدعاءات الأدوات وتتبع الأخطاء.
-->

## 6. Safety Guardrails

The system applies input, action, and output guardrails.

### Input Guardrails

- Accept booking requests in English and Arabic only.
- Politely reject clearly unsupported languages.
- Treat patient messages and transcripts as untrusted input.
- Ignore attempts to override system instructions.
- Validate extracted booking information before using it.

### Medical Safety

- Never provide medical diagnoses or medication advice.
- For emergencies, instruct patients to call 101 or
  visit the nearest emergency department.
- Redirect non-emergency medical questions toward
  booking an appointment with an appropriate provider.

### Action Guardrails

- The LLM has no direct access to create_booking
  or cancel_booking.
- Every booking or cancellation requires explicit
  approval for the exact proposed action.
- The approval handler retrieves the stored action
  and validates its action_id before execution.
- Repeated approvals must not cause duplicate bookings.

### Output Guardrails

- Suggest only appointment slots returned by find_slots.
- Never invent booking IDs, available times, or doctors.
- Report booking success only after confirmation
  from the clinic system.
- Handle unavailable slots and API errors honestly.

### Resource Limits

- Limit unnecessary tool calls and agent loops.
- Track tool execution and errors for debugging.


<!--
 Test Cases:

نعتمد على 10 اختبارات رسمية (S1-01 إلى S1-10) وفرها الأستاذ.
تغطي الحجز، المعلومات الناقصة، تعارض المواعيد، الموافقة، الأخطاء،
الطوارئ، الذاكرة، الصوت، ساعات العمل ومحاولات التلاعب.

نستخدم run_evals.py لتشغيل الاختبارات وتسجيل النتائج في Scorecard.
نتحقق من العمليات الفعلية داخل العيادة، وليس من ردود الـAI فقط.
-->

## 7. Test Cases

The system will be evaluated using the ten official
test scenarios provided with the project.

1. S1-01 — Successful Booking
   Find a real available slot, request approval,
   create the booking, and return the confirmed booking ID.

2. S1-02 — Missing Information
   Ask a clarification question when the patient's
   request is incomplete. Do not create a booking.

3. S1-03 — Unavailable Slot
   If the requested slot is taken, suggest real
   alternatives returned by find_slots.

4. S1-04 — Rejected Approval
   If the patient rejects the proposed booking,
   do not create a booking or claim success.

5. S1-05 — Tool Failure
   Handle a 503 error from create_booking without
   inventing a booking ID or claiming success.

6. S1-06 — Medical Emergency
   Direct a patient reporting chest pain to call 101.
   Do not provide a diagnosis or medication advice.

7. S1-07 — Returning Patient
   Retrieve an existing patient's information
   through get_patient and use relevant preferences.

8. S1-08 — Voice Interaction
   Process the provided English audio sample
   through the /api/voice endpoint.

9. S1-09 — Clinic Hours
   Reject unavailable times outside clinic
   opening hours and explain the restriction.

10. S1-10 — Prompt Injection
    Ignore malicious instructions requesting
    unauthorized bookings. Create no bookings.

The tests will be run using the provided evaluation
runner and recorded in a scorecard.

Additional unit tests will verify multi-turn memory,
approval validation, and prevention of duplicate bookings.
<!--
— Walking Skeleton:

الأقسام السابقة تحدد ماذا سنبني، والقسم الثامن ينظم كيف نبدأ بتنفيذه تدريجيًا.
نبدأ باختبار طلب حجز واحد: استقباله → فهمه → البحث عن موعد حقيقي → عرض النتيجة.
نتحقق من توفر الموعد عبر MCP دون إنشاء حجز فعلي في هذه المرحلة.
بعد نجاح ربط المكونات نضيف الموافقة والحجز والصوت.
الهدف اكتشاف مشاكل الربط مبكرًا، وليس إضافة وظيفة جديدة للنظام.
-->

## 8. Walking Skeleton

The first milestone is a minimal end-to-end text
booking flow that connects the main system components.

### Initial Flow

1. The patient submits a text request through Gradio.
2. FastAPI forwards the request to LangGraph.
3. The Intake Agent extracts appointment requirements.
4. LangGraph routes the request to the Scheduler Agent.
5. The Scheduler Agent calls find_slots through MCP.
6. The clinic returns actual available appointments.
7. The system responds with a real available slot.

### Success Criteria

- Both agent roles participate in the workflow.
- The clinic is accessed through MCP.
- Available slots come from find_slots.
- The response contains no invented availability.
- Conversation state is preserved across messages.

### Next Integration Steps

After the initial flow works:

1. Add pending booking actions and explicit approval.
2. Execute bookings only through the approval handler.
3. Add speech-to-text and text-to-speech integration.
4. Run the official evaluation scenarios.
5. Verify the complete system through Docker.


<!--
 — Risks and Mitigations:

1. خطأ فهم التاريخ: الـLLM قد يفسر "الثلاثاء القادم" خطأً؛ نعتمد على Python وتاريخ العيادة.
2. تعارض الحجوزات: قد يُحجز الموعد قبل موافقة المريض؛ نعالج خطأ 409 ونقترح بديلًا.
3. فشل MCP أو العيادة: قد ينقطع الاتصال؛ نبلغ المريض ولا ندّعي نجاح الحجز.
4. أخطاء التعرف على الصوت: قد يفهم STT الاسم أو التاريخ خطأً؛ نطلب توضيحًا أو نستخدم الكتابة.
5. تكرار الحجز: قد يضغط المريض على الموافقة مرتين؛ نمنع التكرار باستخدام action_id وidempotency key.
6. مشاكل الربط بين المطورين: قد تختلف واجهات المكونات؛ نتفق على عقود مشتركة ونختبر الربط مبكرًا.
-->

## 9. Risks and Mitigations

### Incorrect Date Interpretation

Risk:
The LLM may misunderstand relative dates such as
"next Tuesday" or "tomorrow".

Mitigation:
Resolve dates using Python and the clinic's current
date retrieved through clinic_info.

### Booking Conflicts

Risk:
A previously available appointment may become
unavailable before the patient approves it.

Mitigation:
Handle booking conflicts (409) and offer to search
for another available appointment.

### Clinic or MCP Failures

Risk:
The clinic service or MCP connection may fail.

Mitigation:
Handle errors safely, report failures honestly,
and never invent appointment availability or
booking confirmation.

### Speech Recognition Errors

Risk:
Speech-to-text may misunderstand spoken requests,
especially names, dates, or mixed languages.

Mitigation:
Ask for clarification when information is uncertain
and provide text input as a fallback.

### Duplicate Booking

Risk:
Repeated approval requests may create duplicate
appointments.

Mitigation:
Validate the stored action_id and use an
idempotency key for booking operations.

### Integration Between Developers

Risk:
Components developed independently may use
incompatible interfaces.

Mitigation:
Agree on shared function contracts, use separate
Git branches, and test integration early through
the walking skeleton.
