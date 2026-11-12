# تقرير إصلاح الصوت والمحادثة — 30 أكتوبر 2026

تم العمل على `feat/voice-integration` دون commit أو push أو merge أو PR، ودون تثبيت
اعتمادات أو تنزيل أصوات أو نماذج. ملفات النسخ الأربع بقيت بمحتواها وSHA256 السابقين.
لا توجد كتابة إلى عيادة حقيقية أو محاكية؛ اختبارات الكتابة تستخدم كائنات FakeGateway محلية.

## الأسباب والأدلة

| المشكلة | السبب الذي أمكن إثباته | التصحيح وحدوده |
|---|---|---|
| العربي ظهر إنجليزيًا | UI وVoiceAdapter لم يمررا language_hint رغم دعم STT له؛ أي تفريغ غير فارغ كان يصل إلى Graph، فيستنتج Intake اللغة من النص الإنجليزي | اختيار لغة التسجيل صراحة، تمريرها للمزود والتحقق من خط التفريغ قبل Graph. لا نعيد ترجمة تفريغ خاطئ. لا يثبت هذا دقة الصوت أو كل الكلمات |
| نموذج الصوت الاحتياطي | Voxtral Small 24B 2507 الافتراضي لا يعلن العربية ضمن لغاته؛ لا نملك سجلًا يثبت أنه عالج تسجيل المستخدم السابق | يُمنع هذا fallback في الوضع العربي فقط. المرجع: https://huggingface.co/mistralai/Voxtral-Small-24B-2507 |
| الخدمة الصريحة تُسأل مجددًا | إعادة إنتاج حية قبل الإصلاح: LLM أعاد dentist appointment لطلبي dental checkup، وNone لـcheck up؛ مطابقة النص الأصلي بالكتالوج كانت صحيحة | تعديل تعليمات الاستخراج، ثم explicit_service_evidence من النص الأصلي مع المطابقة اللاحقة بالكتالوج. الاختصاص العام لا يتحول لفحص، وتعدد الإجراءات يبقى غامضًا |
| تكرار الأسئلة أو ضياع الاختيار | تغيير الصياغة لنفس الخدمة/الوقت كان يبطل قائمة سليمة؛ لا يوجد تنبيه عند فقد RAM، وكان هناك fallback لمعرف جلسة مشترك عند غياب session_hash | عدم إبطال الخدمة/الوقت المتكافئين، رفض معرف واجهة مفقود، وإضافة continuation/session_reset. لم نثبت أن فقد RAM هو سبب كل تجربة سابقة؛ التخزين لا يزال مؤقتًا |
| الوقت المحدد غير متاح | Scheduler كان ينهي البحث بـno_slots؛ كان هذا صحيحًا لغياب التطابق لكنه لا يقترح بدائل قريبة | بحث قراءة إضافي ضمن ساعتين قبل/بعد الوقت، في اليوم والخدمة وسياسة الطبيب نفسها، وترتيب القرب، دون اختيار تلقائي أو توسيع after/before |
| الوقت/التاريخ المنطوق | الأرقام الإنجليزية بالكلمات وo'clock وأيام الشهر مثل second لم تكن تُطبّع؛ قد يبقى الوقت ملتصقًا بالتاريخ | فصل التاريخ/الوقت، دعم الكلمات في سياق تاريخ/ساعة، وإبقاء three o'clock غامضة دون AM/PM |
| اختيار طبيعي وعرض عربي | المقارنة بالوقت/الاسم تحتاج سياق القائمة الحالية؛ IDs والأسماء التقنية لا تناسب صوت المريض | عرض موحد وترقيم القائمة الحالية، مطابقة وقت وطبيب موثقين، وحفظ مرشحي الالتباس. الاسم غير الموجود بالكتالوج لا يمر |

## الملفات والدوال المهمة

هذه قائمة التغييرات المتراكمة في حزمة إصلاح المحادثة والصوت الحالية؛ بعضها كان موجودًا
كعمل غير ملتزم من المرحلة السابقة، وتم الحفاظ عليه والتحقق منه، وليس كله تغييرًا جديدًا في هذه الجولة.

| الملف | الدالة/الحقول ومعناها |
|---|---|
| `voice/stt.py` | `_openrouter(data, fmt, language_hint)` يضيف لغة التسجيل للتعليمات ويمنع fallback المذكور للعربية؛ `transcribe(audio, language_hint)` يحافظ على اختيار المزود الموجود |
| `integrations/voice_adapter.py` | `VoiceAdapter.transcribe(data, language_hint)` يتحقق من الملف والمهلة والنص وخط اللغة؛ لا يعرض تفريغًا خاطئ اللغة أو يرسله إلى Graph. `synthesize(reply)` يعيد حقول الصوت دون إعادة معالجة الحجز |
| `ui/app_ui.py` | `send_voice(audio_path, history, request, language)` يرسل اللغة والتسجيل ونفس الجلسة؛ `send_text` يرسل continuation؛ `session_id(request)` يستعمل session_hash؛ `show` يعرض التنبيه والموافقة ولغة TTS؛ `SPEAK_JS` يختار صوت ar/en المتاح |
| `app/main.py` | `voice` يستقبل language_hint وcontinuation في multipart؛ `process_message(session_id, message, gateway, continuation, input_language)` يقفل الجلسة أثناء المعالجة ويكشف فقدها؛ `voice_error_response` يحافظ على الموافقة وحالة الجلسة عند فشل الصوت |
| `core/session_store.py` | `contains(session_id)` يفحص وجود الجلسة دون إنشاء واحدة؛ get_or_create/save يستعملان نسخًا عميقة تحت Lock |
| `core/contracts.py` | حقلا الرد الاختياريان language وsession_reset؛ العقود السابقة تبقى متوافقة |
| `agent/runner.py` | `handle_message(session_id, message, gateway, input_language)` يسترجع الحالة ويشغل graph.ainvoke ثم يحفظها؛ يمسح تلميح لغة التسجيل للدور النصي التالي |
| `agent/state.py` | الكتالوجان للعرض والمطابقة، selection_candidates لرد الطبيب بعد التباس الوقت، current_input_language للدور الحالي، offer_context لتوثيق البدائل |
| `agent/intake.py` | `IntakeResult.separate_date_and_time()` يفصل ساعة كاملة؛ `extract_request(message)` يعطي الإجراء الصريح أولوية؛ `update_state_from_intake(state, result)` يدمج المذكور فقط ويتجنب إبطال قيم متكافئة؛ `is_slot_selection_reply` يفصل اختيار القائمة عن تغيير البحث |
| `agent/service_mapping.py` | `explicit_service_evidence(message)` يعيد نص إجراء صريح لا ID؛ `match_service(service_text, services)` يتحقق من الكتالوج والتفرد؛ `normalize_service_name` يوحد أسنان/الأسنان؛ `service_question(state)` يسأل بخدمات حقيقية فقط |
| `agent/dates.py` | `normalize_date(text, clinic_today)` و`parse_arabic_calendar_date` يستعملان تاريخ العيادة ويحتفظان برفض الماضي وتعارض اليوم؛ ENGLISH_ORDINALS تدعم أيام الشهر المنطوقة |
| `agent/times.py` | `normalize_time(time_text)` يعيد kind وrequested_time/after/before؛ يدعم الإنجليزية والعربية ويحافظ على الفرق بين 15:00 بالضبط وبعدها |
| `agent/scheduler.py` | `schedule(state, gateway)` يبحث عن تطابق أولًا، ثم بدائل وقت محدد إن لزم؛ `filter_matching_slots(state, slots)` يعيد التحقق محليًا حتى لو أعاد MCP صباحًا أو يومًا/خدمة خطأ |
| `agent/slot_handoff.py` | `slot_matches_request(state, slot)` يبقى صارمًا؛ `scheduling_signature(state)` بصمة القيود؛ `slot_matches_offer(state, slot)` يقبل بديلًا موثقًا بالقائمة الحالية فقط ويرفض السياق القديم |
| `agent/slot_selection.py` | `format_slot_options(slots, state)` يرقم نفس ترتيب القائمة؛ `extract_option_numbers(message)` يفهم option two/الخيار الثاني؛ `filter_by_provider` و`offered_clock_times` و`select_slot(state, message)` يتحققون من تقاطع الرقم والوقت والطبيب دون تخمين |
| `agent/presentation.py` | `say(state, english, arabic)` يختار لغة الرد؛ display_date/display_time/provider_label/service_label/booking_summary تعرض أسماء وتواريخ مفهومة دون IDs |
| `agent/graph.py` | `intake_node` يمرر لغة التسجيل المفحوصة مع إبقاء guardrails؛ عقد التطبيع تحفظ الكتالوج؛ مسار المتابعة يتحقق من القائمة ويطلب بيانات المريض الناقصة فقط |
| `actions/approval.py` | `prepare_booking_action(state)` يعد ملخصًا وpayload بعد تحقق الموعد المعروض؛ `handle_approval(...)` يعيد التحقق، مع إبقاء الموافقة الصريحة والإديمبوتنسي ومسار الكتابة الموجود |
| الاختبارات | `test_comprehensive_conversation.py` و`test_arabic_conversation.py` تغطي الحالات المبلغ عنها؛ تعديلات test_numbered_slots/test_voice_api/test_voice_ui توافق العرض الطبيعي وتمرير اللغة |
| الوثائق | `ARABIC_UX.md` و`VOICE_DEMO.md` و`app/README.md` تشرح التشغيل واللغة والقيود واختبار عدم الكتابة |

## تدفق البيانات والحالة

1. Gradio يسجل WAV ويرسل نفس session_id ولغة التسجيل إلى `/api/voice`.
2. VoiceAdapter ينتظر STT بواسطة `asyncio.to_thread`: طلب المزود متزامن لذلك ننقله إلى
   thread، و`await` ينتظر النتيجة دون حجب event loop. المهلة لا تستطيع إيقاف thread بالقوة.
3. النص المقبول يصل إلى نفس process_message المستخدم للنص؛ قفل لكل جلسة يمنع تداخل أدوارها.
4. Intake يستخرج ويُدمج المذكور فقط؛ Graph يتحقق من السلامة ثم تاريخ العيادة والكتالوج والوقت.
5. Scheduler يحفظ proposed_slots وoffer_context، والمرحلة تصبح slots_proposed.
   requested_time الأصلي لا يتغير حين يعرض بدائل، وselected_slot_id يبقى None.
6. «الخيار الثاني» يشير إلى proposed_slots[1]؛ الوقت والطبيب الإضافيان يجب أن يطابقاه.
   الالتباس لا يختار، بل يحفظ selection_candidates ويسأل للتوضيح.
7. الاسم ثم الهاتف يكملان نفس الاختيار؛ تغيير رقم قديم يبطل الهوية السابقة حسب قواعد المشروع.
8. اكتمال البيانات ينشئ pending_action والمرحلة awaiting_approval. قول yes/نعم لا يحجز.
   زر Approve يمر بمسار الموافقة الموجود، ولم يُستخدم مع العيادة في هذه المهمة.
9. الرد وlanguage يرجعان إلى Gradio؛ speechSynthesis ينطق باللغة المناسبة إن توفر صوت.
   لا streaming أو WebSocket أو محرك صوت جديد في هذا التغيير.

## نتائج التحقق وحدوده

- اختبارات unittest الأساسية: 58 حالة، وتشمل 11 حالة جديدة في الملف الشامل.
- اختبارات smoke المستقلة: test_api.py، test_slot_graph_followup.py،
  test_graph_time_filter.py، test_final_handoff.py. كتابات النجاح/التكرار فيها محلية وهمية.
- فحص LLM فعلي بعد الإصلاح: I need a dental checkup / dental checkup / check up
  أعادت جميعها خدمة قابلة للمطابقة مع dental_checkup.
- فحص HTTP/Graph مع LLM الفعلي وعيادة وهمية للقراءة: طلب dentist + التاريخ المنطوق
  + after three PM → توضيح خدمة → check up → خمسة خيارات بعد الثالثة → option two
  → بيانات المريض → awaiting_approval، مع حفظ الوقت والموعد. كل الأدوار HTTP 200.
- الطلب العربي المحدد كاملًا وصل slots_proposed بالعربية مع وقت exact=15:00 وتاريخ
  2026-11-02. هذه مواعيد العيادة الوهمية للاختبار وليست دليل توافر فعلي.
- `clinic_writes=[]` في الفحص الفعلي المذكور. لا تسجيل مستخدم أو سماع متصفح في هذا الفحص.
- اختبار فشل متوقع المرحلة صحح ليقبل awaiting_slot_selection عند تكرار الخدمة؛
  هذا انتظار اختيار من القائمة وليس رجوعًا لسؤال الخدمة. لا فشل وظيفي باقٍ في المجموعة النهائية.

التسجيل العربي والإنجليزي الحقيقيان وإخلاص التفريغ والنطق المسموع ما زالت تحتاج إعادة
تجربة جهاز المستخدم بعد إعادة تشغيل API وGradio. فحص الخط لا يكتشف اسمًا خاطئًا مكتوبًا
بالعربية، ولا يمكن استعادة الكلام الأصلي من ترجمة STT خاطئة. الطبيب غير الموجود بالكتالوج
مرفوض، لكن دقة الاسم المنطوق تحتاج التحقق من transcript والاختيار والموافقة.

## مراجعة Developer A قبل الدمج

- حقول AgentState والعقد الاختيارية وعمر RAM والتعامل مع عدة workers إن أصبح ذلك مطلوبًا.
- offer_context وحدود البدائل ±ساعتين وثبات شروط الطبيب والخدمة واليوم عند الاختيار والموافقة.
- دمج Intake للقيم المتكافئة، مع حماية إبطال الموافقة عند تغير البيانات المؤثرة.
- أولوية guardrails، وصحة payload وaction_id والإديمبوتنسي التي حافظت عليها اختبارات الموافقة.
- تجربة B للميكروفون والنطق العربي/الإنجليزي مع قائمة فعلية حالية، دون الضغط على Approve أثناء الاختبار.
