
# نستورد الدالة التي نريد اختبارها.
import sys

from agent.times import normalize_time

# نجهز أمثلة باللغة العربية والإنجليزية.
tests = [
    "after 3 PM",
    "morning",
    "بعد 3 العصر",
    "قبل الساعة 11 صباحًا",
]

# نجرب تحويل كل مثال ونطبع النتيجة.
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for value in tests:
        print(repr(value), "=>", normalize_time(value))
