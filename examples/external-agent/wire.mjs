// Public schema/ABI translation only. No AgentLance runtime or SDK imports.
import fs from "node:fs";
import { Interface, AbiCoder } from "ethers";
const read = (name) =>
  JSON.parse(fs.readFileSync(new URL("../../specs/" + name, import.meta.url)));
export const schema = read("schemas/protocol.schema.json");
export const catalog = read("catalog.json");
export const writeAbi = new Interface(read("protocol.abi.json"));
export const readAbi = new Interface(read("contracts.read.abi.json"));
const coder = AbiCoder.defaultAbiCoder();
export function ensure(ok, reason = "INVALID_DATA") {
  if (!ok) throw Error(reason);
}
export function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((k) => [k, canonical(value[k])]),
    );
  return value;
}
export const equal = (a, b) =>
  JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
export const ref = (name) => ({ $ref: "#/$defs/" + name });
function resolve(s) {
  while (s.$ref || (s.allOf && !s.type))
    s = s.$ref ? schema.$defs[s.$ref.split("/").at(-1)] : s.allOf[0];
  return s;
}
function zero(f) {
  if (f.baseType === "tuple") return f.components.map(zero);
  if (f.baseType === "array") return [];
  if (f.type === "address") return "0x" + "00".repeat(20);
  if (f.type === "string") return "";
  if (f.type.startsWith("bytes"))
    return "0x" + "00".repeat(Number(f.type.slice(5)) || 0);
  return f.type === "bool" ? false : 0;
}
export function convert(s, value, f, decoding = false) {
  s = resolve(s);
  if (s.anyOf) {
    if (!decoding)
      return value === null
        ? [false, zero(f.components[1])]
        : [true, convert(s.anyOf[0], value, f.components[1])];
    if (!value[0]) {
      ensure(
        coder.encode([f.components[1]], [value[1]]) ===
          coder.encode([f.components[1]], [zero(f.components[1])]),
        "DIRTY_ABSENT",
      );
      return null;
    }
    return convert(s.anyOf[0], value[1], f.components[1], true);
  }
  if (s.type === "object") {
    const entries = Object.entries(s.properties);
    return decoding
      ? Object.fromEntries(
          entries.map(([k, t], i) => [
            k,
            convert(t, value[i], f.components[i], true),
          ]),
        )
      : entries.map(([k, t], i) => convert(t, value[k], f.components[i]));
  }
  if (s.enum) {
    const mapping = [
      "states",
      "terminalReasons",
      "verdicts",
      "counterEffects",
      "allocationOutcomes",
    ]
      .map((k) => catalog[k])
      .find((m) => equal(Object.keys(m).sort(), [...s.enum].sort()));
    ensure(mapping, "ENUM");
    return decoding
      ? Object.keys(mapping).find((k) => BigInt(mapping[k]) === value)
      : mapping[value];
  }
  if (f.type.startsWith("uint") || f.type.startsWith("int"))
    return decoding
      ? s.type === "string"
        ? value.toString()
        : Number(value)
      : BigInt(value);
  return f.type === "address" ? value.toLowerCase() : value;
}
const commands = Object.fromEntries(
  schema.$defs.Command.oneOf.map((s) => [
    s.properties.command.const,
    s.properties.input,
  ]),
);
export function commandData(name, input) {
  const inputs = writeAbi.getFunction(name).inputs;
  return writeAbi.encodeFunctionData(
    name,
    Object.entries(commands[name].properties).map(([k, s], i) =>
      convert(s, input[k], inputs[i]),
    ),
  );
}
export function event(log) {
  const decoded = writeAbi.parseLog(log);
  ensure(
    decoded &&
      writeAbi.encodeEventLog(decoded.fragment, decoded.args).data === log.data,
    "EVENT_ENCODING",
  );
  const spec = schema.$defs.Event.oneOf.find(
    (s) => s.properties.name.const === decoded.name,
  ).properties;
  const flat = {
    type: "object",
    properties: {
      schemaVersion: spec.schemaVersion,
      policyVersion: spec.policyVersion,
      ...spec.payload.properties,
    },
  };
  const value = convert(
    flat,
    decoded.args[0],
    decoded.fragment.inputs[0],
    true,
  );
  const { schemaVersion, policyVersion, ...payload } = value;
  return { schemaVersion, policyVersion, name: decoded.name, payload };
}
export function taskView(raw) {
  const fields = readAbi.getFunction("readTask").outputs[0];
  const values = readAbi.decodeFunctionResult("readTask", raw)[0];
  ensure(
    readAbi.encodeFunctionResult("readTask", [values]) === raw,
    "READ_ENCODING",
  );
  const types = {
    task: ref("TaskSpec"),
    status: { enum: Object.keys(catalog.states) },
    allocation: { anyOf: [ref("Allocation"), { type: "null" }] },
    winningBid: { anyOf: [ref("Bid"), { type: "null" }] },
    result: { anyOf: [ref("ResultCommitment"), { type: "null" }] },
    receipt: { anyOf: [ref("SettlementReceipt"), { type: "null" }] },
  };
  return Object.fromEntries(
    fields.components.map((f, i) => [
      f.name,
      types[f.name]
        ? convert(types[f.name], values[i], f, true)
        : ["childrenCreated", "activeChildren"].includes(f.name)
          ? Number(values[i])
          : values[i].toString(),
    ]),
  );
}
// Bounded recursive JSON parser: rejects duplicate keys and non-integer lexical numbers.
export function strictJson(raw) {
  const text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
  let pos = 0;
  const space = () => {
    while (/\s/.test(text[pos] || "") && pos < text.length) pos++;
  };
  function parse(depth = 0) {
    ensure(depth <= 64, "JSON_DEPTH");
    space();
    const c = text[pos];
    if (c === '"') {
      const start = pos++;
      while (pos < text.length) {
        if (text[pos++] === '"') return JSON.parse(text.slice(start, pos));
        if (text[pos - 1] === "\\") pos++;
      }
      throw Error("JSON_STRING");
    }
    if (c === "{" || c === "[") {
      pos++;
      const object = c === "{",
        result = object ? Object.create(null) : [],
        end = object ? "}" : "]";
      space();
      if (text[pos] === end) {
        pos++;
        return result;
      }
      while (true) {
        if (object) {
          const key = parse(depth + 1);
          ensure(
            typeof key === "string" && !Object.hasOwn(result, key),
            "JSON_KEY",
          );
          space();
          ensure(text[pos++] === ":");
          result[key] = parse(depth + 1);
        } else result.push(parse(depth + 1));
        space();
        const separator = text[pos++];
        if (separator === end) return result;
        ensure(separator === ",");
      }
    }
    for (const [word, value] of [
      ["null", null],
      ["true", true],
      ["false", false],
    ])
      if (text.startsWith(word, pos)) {
        pos += word.length;
        return value;
      }
    const match = text.slice(pos).match(/^-?(0|[1-9][0-9]*)/);
    ensure(match, "JSON_VALUE");
    pos += match[0].length;
    const n = Number(match[0]);
    ensure(Number.isSafeInteger(n), "JSON_INTEGER");
    return n;
  }
  const result = parse();
  space();
  ensure(pos === text.length, "JSON_TRAILING");
  return result;
}
