
# ==================================================
# تذكير — SessionStore
#
# AgentState يحدد شكل بيانات المحادثة.
# SessionStore يحتفظ بالقيم الفعلية لكل session_id.
# get_or_create: يسترجع الحالة أو ينشئ حالة جديدة.
# save: يحفظ الحالة بعد تحديثها.
# delete: يحذف حالة محادثة معينة.
#
# التخزين مؤقت في ذاكرة البرنامج (RAM).
# ==================================================

from copy import deepcopy
from threading import Lock

from agent.state import AgentState, create_initial_state


class SessionStore:

    # إنشاء مخزن للحالات وقفل لحماية عملياته.
    def __init__(self):
        self._sessions: dict[str, AgentState] = {}
        self._lock = Lock()

    # استرجاع حالة موجودة أو إنشاء حالة لمحادثة جديدة.
    def get_or_create(self, session_id: str) -> AgentState:

        if not session_id or not session_id.strip():
            raise ValueError("session_id is required")

        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = create_initial_state(
                    session_id
                )

            return deepcopy(self._sessions[session_id])

    # حفظ التغييرات التي حدثت على حالة المحادثة.
    def save(self, state: AgentState) -> None:

        session_id = state.get("session_id")

        if not session_id:
            raise ValueError("State must contain session_id")

        with self._lock:
            self._sessions[session_id] = deepcopy(state)

    # حذف حالة محادثة من الذاكرة.
    def delete(self, session_id: str) -> None:

        with self._lock:
            self._sessions.pop(session_id, None)


# ==================================================
# مخزن مشترك يستخدمه Runner ونظام الموافقة.
#
# ننشئ الكائن بعد تعريف SessionStore.
# كلا الملفين يستوردان نفس الكائن.
# وهكذا يشتركان في بيانات المحادثات نفسها.
# ==================================================

session_store = SessionStore()
