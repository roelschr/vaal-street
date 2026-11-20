// A trusted local strategy for `npm run replay -- --module examples/rotation.js`.
// Invoked at the close. Return true to target the asset, false to target Chaos.
export default function rotation({ i, close, previousClose }) {
  return i>=24 && close*1000n>previousClose(24)*1005n;
}
