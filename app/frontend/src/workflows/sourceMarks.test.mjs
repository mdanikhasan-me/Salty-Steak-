import assert from "node:assert/strict";
import test from "node:test";

import { faviconAddress, sourceMarks } from "./responseProvenance.mjs";

function turn(sources) {
  return { orchestration: { kind: "research", sources } };
}

test("one mark per site, not per page", () => {


  const marks = sourceMarks(
    turn([
      { url: "https://www.ryans.com/a", title: "A" },
      { url: "https://www.ryans.com/b", title: "B" },
      { url: "https://www.binarylogic.com.bd/c", title: "C" },
    ]),
  );
  assert.deepEqual(
    marks.marks.map((mark) => mark.host),
    ["ryans.com", "binarylogic.com.bd"],
  );
  assert.equal(marks.sites, 2);
  assert.equal(marks.pages, 3);
});

test("a mark points at the page that was read, not at a homepage", () => {
  const marks = sourceMarks(turn([{ url: "https://www.ryans.com/deep/page", title: "T" }]));
  assert.equal(marks.marks[0].url, "https://www.ryans.com/deep/page");
  assert.equal(marks.marks[0].letter, "R");
});

test("the icon comes from the site itself", () => {
  assert.equal(
    faviconAddress("https://www.ryans.com/deep/page"),
    "https://www.ryans.com/favicon.ico",
  );

  assert.equal(faviconAddress("not a url"), "");
  assert.equal(faviconAddress("file:///C:/secret.txt"), "");
  assert.equal(faviconAddress("javascript:alert(1)"), "");
});

test("more sites than fit are counted, not dropped silently", () => {
  const many = Array.from({ length: 7 }, (_, index) => ({
    url: `https://shop${index}.example.com/item`,
    title: `Shop ${index}`,
  }));
  const marks = sourceMarks(turn(many));
  assert.equal(marks.marks.length, 4);
  assert.equal(marks.overflow, 3);
  assert.equal(marks.sites, 7);
});

test("a turn that read nothing shows nothing", () => {
  assert.deepEqual(sourceMarks({}), {
    marks: [],
    overflow: 0,
    sites: 0,
    pages: 0,
  });
  assert.deepEqual(sourceMarks(turn([{ title: "no address" }])).marks, []);
});
