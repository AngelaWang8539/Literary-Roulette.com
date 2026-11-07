import unittest

import textproc
from tests.fixtures import fake_gutenberg


class CleaningTests(unittest.TestCase):
    def setUp(self):
        self.raw = fake_gutenberg(paragraphs=120, seed=3)
        self.text, self.info = textproc.clean_book_text(self.raw)

    def test_wrapper_front_and_back_matter_removed(self):
        for junk in ("Project Gutenberg", "CONTENTS", "PREFACE", "A short note.", "THE END", "Transcriber", "License text", "CHAPTER"):
            self.assertNotIn(junk, self.text)

    def test_story_text_kept(self):
        self.assertGreater(textproc.word_count(self.text), 3000)

    def test_missing_markers_reported_not_guessed(self):
        text, info = textproc.clean_book_text("Just some text with no Gutenberg markers.")
        self.assertIsNone(text)
        self.assertTrue(info["warnings"])

    def test_start_pattern_is_honoured(self):
        raw = fake_gutenberg(paragraphs=40, seed=4).replace("CHAPTER I.\n\n", "CHAPTER I.\n\nOpening words of the tale. " + "x " * 5, 1)
        text, info = textproc.clean_book_text(raw, start_pattern="Opening words of the tale")
        self.assertTrue(text.startswith("Opening words of the tale"))
        text, info = textproc.clean_book_text(raw, start_pattern="This phrase is not in the book")
        self.assertTrue(info["warnings"])  # fallback is reported

    def test_header_fields(self):
        h = textproc.header_fields(self.raw)
        self.assertEqual((h["title"], h["author"], h["language"]), ("Test Tale", "Ann Author", "English"))

    def test_underscores_and_illustration_tags_removed(self):
        raw = fake_gutenberg(paragraphs=30, seed=5).replace("CHAPTER I.\n\n", "CHAPTER I.\n\n[Illustration: a lamp]\n\nHe said _hello_ there [1] and left.\n\n", 1)
        text, _ = textproc.clean_book_text(raw, start_pattern="said")
        self.assertIn("He said hello there and left.", text)
        self.assertNotIn("Illustration", text)


class PassageTests(unittest.TestCase):
    def setUp(self):
        self.text, _ = textproc.clean_book_text(fake_gutenberg(paragraphs=200, seed=7))
        self.passages = textproc.split_passages(self.text)

    def test_sizes_respect_target_range(self):
        counts = [textproc.word_count(p) for p in self.passages]
        self.assertTrue(all(40 <= c <= 200 for c in counts), (min(counts), max(counts)))
        in_range = sum(80 <= c <= 150 for c in counts) / len(counts)
        self.assertGreater(in_range, 0.7)

    def test_deterministic_and_nothing_lost_in_the_middle(self):
        self.assertEqual(self.passages, textproc.split_passages(self.text))
        joined = " ".join(" ".join(p.split()) for p in self.passages)
        sample = " ".join(self.text.split()[500:520])
        self.assertIn(sample, joined)

    def test_long_paragraph_is_split_on_sentences(self):
        long_par = " ".join(f"Sentence number {i} ends here." for i in range(120))
        out = textproc.split_passages(long_par)
        self.assertGreater(len(out), 2)
        self.assertTrue(all(p.rstrip().endswith(".") for p in out))

    def test_verse_keeps_line_breaks(self):
        verse = "\n\n".join("\n".join(f"Line {j} of stanza {i} sings" for j in range(4)) for i in range(25))
        self.assertIn("\n", textproc.split_passages(verse)[0].split("\n\n")[0])

    def test_short_text_does_not_crash(self):
        self.assertEqual(textproc.split_passages(""), [])
        self.assertEqual(textproc.split_passages("Too short."), [])


if __name__ == "__main__":
    unittest.main()
