"""Turn pytest's JUnit XML into GitHub annotations, so failures show on the run's page.

GitHub only shows raw logs to people who are signed in, but anyone can read annotations.

Joke: if a test fails in the forest and nobody signs in, it still fails.
"""

import sys
import xml.etree.ElementTree as ET  # nosec: this is pytest's own file, made moments earlier

# GitHub shows at most 10 error annotations per step, and cuts long messages.
MAX_FAILURES = 10
MAX_MESSAGE_CHARS = 3000


def escape(text: str) -> str:
    """Workflow commands would otherwise end the message at the first newline."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: str) -> int:
    failures = []
    for case in ET.parse(path).getroot().iter("testcase"):
        for problem in list(case.findall("failure")) + list(case.findall("error")):
            name = f"{case.get('classname')}.{case.get('name')}"
            failures.append((name, problem.text or problem.get("message") or "no details"))
    print(f"{len(failures)} test(s) failed")
    for name, details in failures[:MAX_FAILURES]:
        # The end of a traceback says the most, so that's the part to keep.
        print(f"::error title={escape(name)}::{escape(details[-MAX_MESSAGE_CHARS:])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
