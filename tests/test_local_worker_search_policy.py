"""Prompt-policy tests guarding the local-worker SEARCH DISCIPLINE wording.

Protects the exact semantics decided after the live A/B failure (WITH map:
broad "fast_bridge|shm" mega-regex churn; WITHOUT map: 125.58s/31 calls):

1. A mapped exact file path/symbol is a DIRECT Read target, not a search term.
2. No wording that encourages alternation mega-regexes ("one consolidated
   query" etc.).
3. One search resolves ONE missing link: narrowest exact identifier, smallest
   relevant subtree; conceptual aliases tried SEQUENTIALLY, never OR-combined.
4. Multiple patterns only for an explicitly requested exhaustive audit, then
   partitioned by named subsystem/question.
5. Bash remains fully available for build/test/diagnostics; grep/rg/find
   inside Bash obey the same search-intent rules and are not forbidden.
6. No numeric budgets, no fixed folder allowlist, no tool denial.
"""

import os
import re
import unittest

PERSONA_PATH = os.path.expanduser("~/.claude/agents/local-worker.md")


def load_discipline_text():
    with open(PERSONA_PATH, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"SEARCH DISCIPLINE.*", text, re.S)
    if not m:
        raise AssertionError("local-worker.md has no SEARCH DISCIPLINE section")
    return m.group(0)


class MappedAnchorIsDirectReadTest(unittest.TestCase):
    def test_mapped_path_or_symbol_is_direct_read_target(self):
        t = load_discipline_text().lower()
        self.assertRegex(
            t,
            r"mapped exact file path or symbol",
            "must name the mapped exact file path/symbol rule",
        )
        self.assertIn("direct read", t)
        self.assertRegex(t, r"not a search term")

    def test_map_stays_hypothesis_not_reconstructed(self):
        t = load_discipline_text().lower()
        self.assertIn("starting hypothesis", t)
        self.assertRegex(t, r"never reconstruct mapped topology")


class NoMegaRegexEncouragementTest(unittest.TestCase):
    def test_no_consolidated_query_wording(self):
        t = load_discipline_text().lower()
        self.assertNotIn("consolidated", t,
                         "'one consolidated query' wording encourages alternation "
                         "mega-regexes and must stay removed")

    def test_no_generic_or_example_pattern(self):
        t = load_discipline_text()
        self.assertNotRegex(t, r"rg\s+-n\s+'a\|b\|c'",
                            "the 'a|b|c' mega-regex example must not remain")


class OneMissingLinkSemanticsTest(unittest.TestCase):
    def test_one_missing_link_narrowest_identifier_smallest_subtree(self):
        t = load_discipline_text().lower()
        self.assertRegex(t, r"one missing link")
        self.assertRegex(t, r"narrowest exact identifier")
        self.assertRegex(t, r"smallest relevant subtree")

    def test_aliases_sequential_never_or_combined(self):
        t = load_discipline_text().lower()
        self.assertRegex(t, r"sequentially only after the previous hypothesis failed")
        self.assertRegex(t, r"never or-combine")


class MultiPatternOnlyForExplicitAuditTest(unittest.TestCase):
    def test_multi_pattern_scoped_to_partitioned_audit(self):
        t = load_discipline_text().lower()
        self.assertRegex(t, r"multiple patterns")
        self.assertRegex(t, r"explicitly requested exhaustive audit")
        self.assertRegex(t, r"partitioned by named subsystem")


class BashAvailabilityTest(unittest.TestCase):
    def test_bash_fully_available_and_same_rules_apply(self):
        t = load_discipline_text().lower()
        self.assertRegex(t, r"bash remains fully available for build, test, and diagnostics")
        self.assertRegex(t, r"grep/rg/find")
        self.assertIn("not forbidden", t)
        self.assertRegex(t, r"same (search )?intent rules")
        self.assertRegex(t, r"bypass")


class NoBudgetsNoDenialsTest(unittest.TestCase):
    def test_explicit_no_budgets_allowlist_or_tool_denial(self):
        t = load_discipline_text().lower()
        self.assertRegex(t, r"no numeric budgets")
        self.assertRegex(t, r"no fixed folder allowlist")
        self.assertRegex(t, r"no tool denial")

    def test_no_numeric_budget_lines_remain(self):
        # Policy must not reintroduce numeric caps like "at most N" queries.
        t = load_discipline_text().lower()
        self.assertNotRegex(t, r"(at most|maximum|cap of)\s+\d")


if __name__ == "__main__":
    unittest.main()