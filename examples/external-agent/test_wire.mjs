import test from "node:test";
import assert from "node:assert/strict";
import { strictJson, commandData, writeAbi, event } from "./wire.mjs";

test("strict JSON rejects ambiguous integers, duplicate keys and excess depth", () => {
  for (const raw of [
    '{"a":1,"a":2}',
    '{"a":1.0}',
    '{"a":1e0}',
    '{"a":NaN}',
    " ".repeat(3) + "1 false",
    "[".repeat(66) + "0" + "]".repeat(66),
  ])
    assert.throws(() => strictJson(Buffer.from(raw)));
  assert.equal(strictJson(Buffer.from('{"value":7}')).value, 7);
});
test("native calldata uses exact uint256 withdrawal amounts and nullable fields", () => {
  const receiver = "0x" + "12".repeat(20),
    amountAtoms = (2n ** 200n).toString();
  const encoded = commandData("withdrawCredit", { receiver, amountAtoms });
  const decoded = writeAbi.decodeFunctionData("withdrawCredit", encoded);
  assert.equal(decoded[1].toString(), amountAtoms);
  const log = writeAbi.encodeEventLog(writeAbi.getEvent("CreditWithdrawn"), [
    [1, 1, receiver, receiver, amountAtoms],
  ]);
  assert.equal(event(log).payload.amountAtoms, amountAtoms);
});
