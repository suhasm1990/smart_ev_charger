"""Chat replies must survive Telegram's strict HTML parser."""
import tests  # noqa: F401  -- must stay first: isolates Sheets and log files (see tests/__init__.py)

# isort: split
import unittest

from agent.telegram_bot import clean_telegram_html
from reporting.notifications import strip_html

LABEL = "🤖 <i>Model: nvidia/nemotron-3-ultra-550b-a55b</i>"


class TestCleanTelegramHtml(unittest.TestCase):
    def test_markdown_becomes_html(self):
        out = clean_telegram_html("## Charger Status\n- **State:** `IDLE`\n- *charging* from solar")
        self.assertEqual(out, "<b>Charger Status</b>\n• <b>State:</b> <code>IDLE</code>\n• <i>charging</i> from solar")

    def test_stray_angle_brackets_are_escaped(self):
        out = clean_telegram_html(f"- **Stop threshold:** < 35% battery & > 2 kW\n\n{LABEL}")
        self.assertIn("<b>Stop threshold:</b> &lt; 35% battery &amp; &gt; 2 kW", out)
        self.assertTrue(out.endswith(LABEL))

    def test_existing_entities_are_not_double_escaped(self):
        self.assertEqual(clean_telegram_html("A &amp; B"), "A &amp; B")

    def test_unsupported_tags_become_line_breaks(self):
        self.assertEqual(clean_telegram_html("<p>one</p><p>two</p>"), "one\n\ntwo")


class TestStripHtml(unittest.TestCase):
    def test_keeps_text_after_a_bare_less_than(self):
        plain = strip_html(f"<b>Stop threshold:</b> &lt; 35% battery\n\n{LABEL}")
        self.assertEqual(plain, "Stop threshold: < 35% battery\n\n🤖 Model: nvidia/nemotron-3-ultra-550b-a55b")

    def test_unescaped_less_than_also_survives(self):
        self.assertEqual(strip_html("stop < 35% <i>x</i>"), "stop < 35% x")


if __name__ == "__main__":
    unittest.main()
