import { describe, expect, it } from "vitest";
import { createTestIndexer } from "envio";
import { Interface, type ParamType } from "ethers";
import abi from "../../../specs/protocol.abi.json";
import fixtures from "../../../specs/fixtures/objects.json";
import catalog from "../../../specs/catalog.json";
import "../src/handlers";

const wire = new Interface(abi);
const objects = new Map(fixtures.objects.map(entry => [entry.id, entry.value]));
const zeroAddress = "0x" + "00".repeat(20);
const enumValues = Object.assign({}, catalog.states, catalog.terminalReasons, catalog.verdicts,
  catalog.counterEffects, catalog.allocationOutcomes);

function encodeField(field: ParamType, value: unknown): unknown {
  if (field.baseType === "tuple") {
    const fields = field.components!;
    if (fields[0].name === "present") {
      return { present: value !== null, value: encodeField(fields[1], value) };
    }
    const object = (value ?? {}) as Record<string, unknown>;
    return Object.fromEntries(fields.map(child => [child.name, encodeField(child, object[child.name] ?? null)]));
  }
  if (field.type === "address") return value ?? zeroAddress;
  if (field.type.startsWith("bytes")) return value ?? ("0x" + "00".repeat(Number(field.type.slice(5)) || 0));
  if (field.type === "string") return value ?? "";
  if (field.type === "bool") return value ?? false;
  if (typeof value === "string" && value in enumValues) return BigInt(enumValues[value as keyof typeof enumValues]);
  return BigInt((value ?? 0) as string | number);
}

function simulated(name: "TaskCreated" | "BidAccepted" | "TaskAwarded" | "AwardAccepted" | "ResultSubmitted" | "TaskSettled" | "CreditWithdrawn", payload: object, block: number, logIndex = 0) {
  const data = encodeField(wire.getEvent(name)!.inputs[0], { schemaVersion: 1, policyVersion: 1, ...payload });
  return {
    contract: "AgentLanceMarket" as const, event: name, params: { data } as never,
    srcAddress: "0x0000000000000000000000000000000000000064" as const,
    block: { number: block, hash: "0x" + block.toString(16).padStart(64, "0"), timestamp: 1000 + block },
    transaction: { hash: "0x" + (block * 10 + logIndex).toString(16).padStart(64, "0") }, logIndex,
  };
}

function object(name: string): Record<string, unknown> {
  return structuredClone(objects.get(name)) as Record<string, unknown>;
}

describe("canonical event discovery", () => {
  it("retains creation and closes an awarded task once across settlement", async () => {
    const index = createTestIndexer();
    await index.process({ chains: { 31337: { simulate: [simulated("TaskCreated", { task: object("TaskSpec") }, 10)] } } });
    let tasks = await index.MarketTask.getAll();
    expect(tasks).toHaveLength(1);
    expect(tasks[0].closedBlock).toBeUndefined();
    await index.process({ chains: { 31337: { simulate: [simulated("BidAccepted", { bid: object("Bid"), permitNonce: null }, 15)] } } });
    expect((await index.MarketTask.getAll())[0].closedBlock).toBeUndefined();
    await index.process({ chains: { 31337: { simulate: [simulated("TaskAwarded", { allocation: object("Allocation") }, 20)] } } });
    await index.process({ chains: { 31337: { simulate: [
      simulated("AwardAccepted", { executionRef: object("ExecutionRef"), ownWorkReserveAtoms: "0", acceptedAt: "1400" }, 21),
      simulated("ResultSubmitted", { result: object("ResultCommitment") }, 22),
    ] } } });
    await index.process({ chains: { 31337: { simulate: [simulated("TaskSettled", { receipt: object("SettlementReceipt") }, 30)] } } });
    const payout = object("SettlementReceipt").payout;
    await index.process({ chains: { 31337: { simulate: [simulated("CreditWithdrawn", { owner: payout, receiver: payout, amountAtoms: "100" }, 31)] } } });
    tasks = await index.MarketTask.getAll();
    expect(tasks[0].createdBlock).toBe(10n);
    expect(tasks[0].closedBlock).toBe(20n);
    expect(await index.MarketLog.getAll()).toHaveLength(7);
  });

  it("closes an unallocated auction with no award event", async () => {
    const index = createTestIndexer();
    const receipt = { ...object("SettlementReceipt"), reason: "UNALLOCATED", counterEffect: "NONE" };
    await index.process({ chains: { 31337: { simulate: [
      simulated("TaskCreated", { task: object("TaskSpec") }, 10),
      simulated("TaskSettled", { receipt }, 20),
    ] } } });
    expect((await index.MarketTask.getAll())[0].closedBlock).toBe(20n);
  });

  it("keeps root and child discovery separate and rejects foreign references", async () => {
    const index = createTestIndexer();
    const root = object("TaskSpec");
    const child = { ...root, taskRef: { ...(root.taskRef as object), taskId: "2" }, parentRef: root.taskRef, depth: 1 };
    await index.process({ chains: { 31337: { simulate: [
      simulated("TaskCreated", { task: root }, 10), simulated("TaskCreated", { task: child }, 11),
    ] } } });
    expect((await index.MarketTask.getAll()).map(task => task.parentId)).toEqual([undefined, 1n]);
    const foreign = { ...root, taskRef: { ...(root.taskRef as object), chainId: "1" } };
    await expect(index.process({ chains: { 31337: { simulate: [simulated("TaskCreated", { task: foreign }, 30)] } } })).rejects.toThrow();
  });
});
