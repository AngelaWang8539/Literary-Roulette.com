// Book card text tests. Run: node tests/js/bookcard_tests.mjs
import assert from "node:assert/strict";
import { formatAuthor, cleanGenres, teaserFrom } from "../../static/js/bookcard.js";

let n = 0; const eq = (a, b, m) => { assert.deepEqual(a, b, m); n++; };
eq(formatAuthor("Standish, Burt L., 1866-1945; Owen, Robert Emmett, 1878-1957 [Illustrator]"),
  { by: "Burt L. Standish", years: "1866\u20131945", roles: ["Illustrated by Robert Emmett Owen"] });
eq(formatAuthor("Doyle, Arthur Conan, 1859-1930"), { by: "Arthur Conan Doyle", years: "1859\u20131930", roles: [] });
eq(formatAuthor("Arthur Conan Doyle"), { by: "Arthur Conan Doyle", years: "", roles: [] });
eq(formatAuthor("Anonymous"), { by: "Anonymous", years: "", roles: [] });
eq(formatAuthor("Homer, active 9th century B.C."), { by: "Homer", years: "", roles: [] });
eq(formatAuthor("Smith, John, Jr., 1850-1900").by, "John Smith Jr.");
eq(formatAuthor("Austen, Jane, 1775-1817; Brock, C. E. (Charles Edmund), 1870-1938 [Illustrator]; Rhys, Ernest [Editor]").roles,
  ["Illustrated by C. E. (Charles Edmund) Brock", "Edited by Ernest Rhys"]);
eq(formatAuthor("Marx, Karl, 1818-1883; Engels, Friedrich, 1820-1895").by, "Karl Marx & Friedrich Engels");
eq(formatAuthor("Marx, Karl, 1818-1883; Engels, Friedrich, 1820-1895").years, "");
eq(formatAuthor("A, B; C, D; E, F").by, "B A et al.");
eq(formatAuthor("Dumas, Alexandre, 1802-").years, "1802\u2013");
eq(formatAuthor("Translator only [Translator]").by, "Translator only");
eq(formatAuthor(""), { by: "", years: "", roles: [] });
eq(formatAuthor(null).by, "");
eq(cleanGenres(["Category: Humour", "Category: Adventure", "Novels", "humour", "  ", "Category:"]), ["Humour", "Adventure", "Novels"]);
eq(cleanGenres(undefined), []);
eq(teaserFrom("were the tools of a man who had won supremacy with each. Like a dado round the room was the jutting line of splendid heavy game-heads."),
  "Like a dado round the room was the jutting line of splendid heavy game-heads.");
eq(teaserFrom("Short one. \u201cWell, Frank, it seems we have more than mischief to deal with this time,\u201d said Dick."),
  "\u201cWell, Frank, it seems we have more than mischief to deal with this time,\u201d said Dick.");
eq(teaserFrom("no capital start here and nothing that ends well"), "");
eq(teaserFrom(""), "");
eq(teaserFrom("x".repeat(40) + ". " + "He went home and slept well that night, all things considered."), "He went home and slept well that night, all things considered.");
eq(teaserFrom("他走进了那间安静的老图书馆，书架上的灰尘在阳光里慢慢地飘着。"), "他走进了那间安静的老图书馆，书架上的灰尘在阳光里慢慢地飘着。");
console.log("  ok   book card tests (" + n + " checks)");
