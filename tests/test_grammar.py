"""The Python floor, checked against the files this project ships.

RHEL 9 and Debian 11 ship Python 3.9 as the system python3, RHEL 9 is
supported to 2032, and holding that floor costs this project almost nothing:
stdlib only, no type hints, no __future__ imports. The one real constraint is
PEP 701, and it is the one an ordinary syntax check cannot see.

Why a whole file for this: a version-specific SyntaxError is raised at
*import*, not at the call, so the failure is not a broken panel or a wrong
number. It is a service that does not start, on the distributions this project
most expects to be installed on, and never on the machine it was written on.
"""

import ast
import re
import unittest
from pathlib import Path

import _support

SCRIPTS = sorted((_support.REPO / "scripts").glob("*.py"))

# A quote of the enclosing kind, or a backslash, inside the expression part of
# an f-string. Both are PEP 701, which landed in 3.12.
FSTRING_EXPR = re.compile(r"f(['\"])[^{}]*\{([^{}]*)\}")


class TestFloor(unittest.TestCase):

    def test_there_are_scripts_to_check(self):
        # A glob that matches nothing would make every test below vacuous.
        self.assertGreaterEqual(len(SCRIPTS), 6)

    def test_the_grammar_is_3_9(self):
        for path in SCRIPTS:
            with self.subTest(path.name):
                ast.parse(path.read_text(encoding="utf-8"),
                          filename=str(path), feature_version=(3, 9))

    def test_no_pep_701_f_strings(self):
        """The gap in the check above, and the reason this file exists.

        `ast.parse(feature_version=(3, 9))` does *not* reject a nested quote or
        a backslash inside an f-string expression; it parses with the running
        interpreter's tokenizer, which on any modern box is 3.12 or later. So
        the one 3.10+ construct this project can realistically write by
        accident is exactly the one that check waves through.

        Real example, caught by this test on the day it was written:

            f'<td{" class=\\'bad\\'" if bad else ""}>'

        which imports fine on 3.12 and is a SyntaxError on 3.9, in the module
        that serves the web UI. Build the value on its own line instead.
        """
        offenders = []
        for path in SCRIPTS:
            for n, line in enumerate(path.read_text(encoding="utf-8")
                                     .splitlines(), 1):
                for m in FSTRING_EXPR.finditer(line):
                    quote, expr = m.group(1), m.group(2)
                    if "\\" in expr or quote in expr:
                        offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "PEP 701 f-string grammar needs 3.12; "
                                        "build the value outside the f-string")


if __name__ == "__main__":
    unittest.main()
