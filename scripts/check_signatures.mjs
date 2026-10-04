// Independent ethers verification. Never writes or regenerates expected vectors.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { TypedDataEncoder, keccak256, toUtf8Bytes, verifyTypedData } from 'ethers';

const vectors = JSON.parse(fs.readFileSync(new URL('../specs/fixtures/signatures.json', import.meta.url)));
for (const vector of vectors.positive) {
  const { domain, message, primaryType, types } = vector.typedData;
  const messageTypes = { [primaryType]: types[primaryType] };
  const encoder = TypedDataEncoder.from(messageTypes);
  assert.equal(keccak256(toUtf8Bytes(encoder.encodeType(primaryType))), vector.typeHash);
  assert.equal(TypedDataEncoder.hashDomain(domain), vector.domainSeparator);
  assert.equal(encoder.hash(message), vector.structHash);
  assert.equal(TypedDataEncoder.hash(domain, messageTypes, message), vector.digest);
  assert.equal(verifyTypedData(domain, messageTypes, message, vector.signature).toLowerCase(), vector.recoveredSigner);
}
for (const vector of vectors.negative.filter(v => v.mutation !== null)) {
  const base = vectors.positive.find(v => v.id === vector.base);
  const data = structuredClone(base.typedData);
  const { section, field, value } = vector.mutation;
  if (section === 'primaryType') {
    data.types[value] = data.types[data.primaryType];
    delete data.types[data.primaryType];
    data.primaryType = value;
  } else data[section][field] = value;
  const types = { [data.primaryType]: data.types[data.primaryType] };
  assert.notEqual(verifyTypedData(data.domain, types, data.message, base.signature).toLowerCase(), base.recoveredSigner);
}
for (const vector of vectors.contentHashes) assert.equal(keccak256(vector.bytesHex), vector.keccak256);
console.log(`ethers independently verified ${vectors.positive.length} signing vectors, 9 mutations and ${vectors.contentHashes.length} byte hashes.`);
