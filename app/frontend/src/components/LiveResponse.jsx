import { useMemo, useRef } from "react";
import { RichText } from "./RichText.jsx";
import { mergePublicPreview } from "../workflows/publicResponsePreview.mjs";
import { splitAssistantContent } from "../workflows/chatContent.mjs";

export function LiveResponse({ operation }) {
  const previous = useRef(null);
  const details = operation?.result || operation?.progress || {};
  const preview = details.research_progress?.generation_preview || details.generation_preview;
  const route = details.learned_route_controller?.route;
  const researchPhase = details.research_progress?.phase;
  const publicRoute = !route || ["respond", "identity"].includes(route)
    || (route === "research" && ["drafting", "completed"].includes(researchPhase));
  const result = useMemo(() => {
    previous.current = mergePublicPreview(previous.current, preview, operation?.id);
    return previous.current;
  }, [operation?.id, preview]);
  if (!publicRoute || !result) return null;
  const answer = splitAssistantContent(result.text).answer;
  if (!answer) return null;
  return <div className="message__content live-response" aria-label="Response in progress">
    {result.partial ? <><small className="live-response__notice">Latest part of the response. The complete answer will appear when ready.</small><p className="live-response__tail">{answer}</p></> : <RichText>{answer}</RichText>}
  </div>;
}
