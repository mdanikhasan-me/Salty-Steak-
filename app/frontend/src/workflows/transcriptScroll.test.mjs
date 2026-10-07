import assert from "node:assert/strict";
import test from "node:test";
import { isNearTranscriptBottom, shouldFollowTranscript } from "./transcriptScroll.mjs";

test("transcript follows appended output only while the reader is near its end", () => {
  assert.equal(isNearTranscriptBottom({ scrollTop: 440, clientHeight: 500, scrollHeight: 990 }), true);
  assert.equal(isNearTranscriptBottom({ scrollTop: 300, clientHeight: 500, scrollHeight: 990 }), false);
  assert.equal(shouldFollowTranscript({ wasNearBottom: true, contentChanged: true }), true);
  assert.equal(shouldFollowTranscript({ wasNearBottom: false, contentChanged: true }), false);
});
