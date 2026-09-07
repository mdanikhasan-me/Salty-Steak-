const WORK_TYPES=new Set(["chat_generation","chat_image_generation","chat_vision_analysis","chat_agent_task","agent_task","chat_host_action_execution"]);
const PAUSED=/waiting for (?:you|approval|sign|user)|needs.review|user.approval|awaiting/;
export function companionIsWorking(operations, online=true) {
  if (!online) return false;
  return Object.values(operations || {}).some(operation => {
    if(!WORK_TYPES.has(operation?.type) || !["running","stop_requested"].includes(operation.state))return false;
    const state=String(operation.result?.agent_task?.state || operation.progress?.agent_task?.state || "");
    return !PAUSED.test(`${state} ${operation.phase || ""}`.toLowerCase()) && !["waiting","needs_review"].includes(state);
  });
}
