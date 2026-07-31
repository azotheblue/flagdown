/**
 * Flagdown extras — sort challenges by name (not points)
 *
 * Requires CTFd theme: core-beta
 * Paste into: Admin → Config → Theme → Settings Editor
 *
 * Box 1 — Challenge Category Order
 * Box 2 — Challenge Order (within each category)
 */

/* --- Box 1: Challenge Category Order (a/b are category name strings) --- */
function(a, b) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

/* --- Box 2: Challenge Order (a/b are challenge objects with .name / .value) --- */
function(a, b) {
  return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
}

/* Optional alternative for Box 2 — points first, then natural name:
function(a, b) {
  if (a.value !== b.value) return a.value - b.value;
  return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
}
*/
