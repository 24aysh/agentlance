import { indexer, type EvmOnEventContext } from "envio";
import { Interface } from "ethers";
import abi from "../../../specs/protocol.abi.json";

const wire = new Interface(abi);
type Observation = {
  chainId: number;
  srcAddress: string;
  logIndex: number;
  block: { number: number; hash: string };
  transaction: { hash: string };
  params: { data: unknown };
};

async function saveLog(name: string, event: Observation, context: EvmOnEventContext): Promise<string> {
  if (!Number.isSafeInteger(event.block.number) || !Number.isSafeInteger(event.logIndex)) {
    throw new Error("Unsafe event position");
  }
  const market = event.srcAddress.toLowerCase();
  const id = `${event.chainId}:${market}:${event.block.hash}:${event.logIndex}`;
  const encoded = wire.encodeEventLog(name, [event.params.data]);
  const old = await context.MarketLog.get(id);
  if (old && (old.data !== encoded.data || old.transactionHash !== event.transaction.hash || old.eventName !== name)) {
    throw new Error("Conflicting duplicate log");
  }
  context.MarketLog.set({
    id, market,
    blockNumber: BigInt(event.block.number), blockHash: event.block.hash,
    transactionHash: event.transaction.hash, logIndex: BigInt(event.logIndex),
    eventName: name, data: encoded.data, topic: encoded.topics[0],
  });
  return id;
}

function taskKey(ref: { chainId: bigint; market: string; taskId: bigint }, event: Observation): string {
  if (ref.chainId !== BigInt(event.chainId) || ref.market.toLowerCase() !== event.srcAddress.toLowerCase()) {
    throw new Error("Foreign task reference");
  }
  return `${ref.chainId}:${ref.market.toLowerCase()}:${ref.taskId}`;
}

indexer.onEvent({ contract: "AgentLanceMarket", event: "TaskCreated" }, async ({ event, context }) => {
  const creationLog = await saveLog("TaskCreated", event, context);
  const task = event.params.data.task;
  if (task.schemaVersion !== 1n || event.params.data.policyVersion !== 1n) {
    throw new Error("Unsupported market version");
  }
  const id = taskKey(task.taskRef, event);
  const old = await context.MarketTask.get(id);
  if (old) {
    if (old.creationLog !== creationLog) throw new Error("Conflicting task creation");
    return;
  }
  context.MarketTask.set({
    id,
    market: event.srcAddress.toLowerCase(), taskId: task.taskRef.taskId,
    parentId: task.parentRef.present ? task.parentRef.value.taskId : undefined,
    createdBlock: BigInt(event.block.number), closedBlock: undefined, creationLog,
  });
});

indexer.onEvent({ contract: "AgentLanceMarket", event: "TaskAwarded" }, async ({ event, context }) => {
  await saveLog("TaskAwarded", event, context);
  const task = await context.MarketTask.getOrThrow(taskKey(event.params.data.allocation.taskRef, event));
  context.MarketTask.set({ ...task, closedBlock: task.closedBlock ?? BigInt(event.block.number) });
});

indexer.onEvent({ contract: "AgentLanceMarket", event: "TaskSettled" }, async ({ event, context }) => {
  await saveLog("TaskSettled", event, context);
  const task = await context.MarketTask.getOrThrow(taskKey(event.params.data.receipt.taskRef, event));
  context.MarketTask.set({ ...task, closedBlock: task.closedBlock ?? BigInt(event.block.number) });
});

indexer.onEvent({ contract: "AgentLanceMarket", event: "BidAccepted" }, async ({ event, context }) => {
  await saveLog("BidAccepted", event, context);
});
indexer.onEvent({ contract: "AgentLanceMarket", event: "AwardAccepted" }, async ({ event, context }) => {
  await saveLog("AwardAccepted", event, context);
});
indexer.onEvent({ contract: "AgentLanceMarket", event: "ResultSubmitted" }, async ({ event, context }) => {
  await saveLog("ResultSubmitted", event, context);
});
indexer.onEvent({ contract: "AgentLanceMarket", event: "CreditWithdrawn" }, async ({ event, context }) => {
  await saveLog("CreditWithdrawn", event, context);
});
