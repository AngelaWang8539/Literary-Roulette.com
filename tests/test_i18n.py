"""The reveal panel (links, credit line, publication, passage source, genre tags) follows the language."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r"""
const { translate, ENTRIES } = require("./static/js/i18n.js");
const cases = JSON.parse(process.argv[1]);
const out = cases.map(([text, lang]) => translate(text, lang));
const bad = ENTRIES.filter(([en, zh, fr]) => {
  const names = (s) => (s.match(/\{(\w+)\}/g) || []).sort().join();
  return names(en) !== names(zh) || names(en) !== names(fr);
}).map((e) => e[0]);
console.log(JSON.stringify({ out, bad }));
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class RevealPanelTranslationTests(unittest.TestCase):
    def run_node(self, cases):
        res = subprocess.run(["node", "-e", SCRIPT, json.dumps(cases)], cwd=ROOT, capture_output=True, text=True, check=True)
        return json.loads(res.stdout)

    def test_reveal_panel_strings_are_translated(self):
        cases = [
            ["Read online", "zh"], ["Read online", "fr"], ["Borrow", "fr"], ["Buy e-book", "zh"],
            ["Text source: Project Gutenberg eBook #24316", "zh"], ["Text source: Project Gutenberg eBook #60827", "fr"],
            ["Category: Poetry", "zh"], ["Category: Novels", "fr"], ["FR Littérature", "zh"], ["Category: Unlisted Shelf", "fr"],
            ["First published 1920. Publisher: Acme.", "fr"], ["by Jane Austen", "zh"],
            ["This passage: Project Gutenberg eBook #11, Passage 4.", "zh"], ["Read online", "en"],
        ]
        data = self.run_node(cases)
        out = dict(zip(map(tuple, cases), data["out"]))
        self.assertEqual(out[("Read online", "zh")], "在线阅读")
        self.assertEqual(out[("Read online", "fr")], "Lire en ligne")
        self.assertEqual(out[("Borrow", "fr")], "Emprunter")
        self.assertEqual(out[("Buy e-book", "zh")], "购买电子书")
        self.assertIn("#24316", out[("Text source: Project Gutenberg eBook #24316", "zh")])
        self.assertTrue(out[("Text source: Project Gutenberg eBook #24316", "zh")].startswith("文本来源"))
        self.assertTrue(out[("Text source: Project Gutenberg eBook #60827", "fr")].startswith("Source du texte"))
        self.assertEqual(out[("Category: Poetry", "zh")], "类别：诗歌")
        self.assertEqual(out[("Category: Novels", "fr")], "Catégorie : Romans")
        self.assertEqual(out[("FR Littérature", "zh")], "法语文学")
        self.assertEqual(out[("Category: Unlisted Shelf", "fr")], "Catégorie : Unlisted Shelf")
        self.assertEqual(out[("First published 1920. Publisher: Acme.", "fr")], "Première publication en 1920. Éditeur : Acme.")
        self.assertEqual(out[("by Jane Austen", "zh")], "作者：Jane Austen")
        self.assertEqual(out[("This passage: Project Gutenberg eBook #11, Passage 4.", "zh")], "本段文字：Project Gutenberg 电子书 #11，第 4 段。")
        self.assertEqual(out[("Read online", "en")], "Read online")

    def test_every_entry_keeps_the_same_placeholders_in_all_languages(self):
        self.assertEqual(self.run_node([])["bad"], [])


if __name__ == "__main__":
    unittest.main()
