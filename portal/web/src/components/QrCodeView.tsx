import { useMemo } from "react";

// Simple, self-contained SVG QR matrix generator for standard pairing URIs
function generateQrMatrix(text: string): boolean[][] {
  // Galois Field & Reed-Solomon tables for QR Version 2-3
  const EXP = new Array(512);
  const LOG = new Array(256);
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];

  const gfMul = (a: number, b: number) => (a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]]);

  const rsPoly = (degree: number) => {
    let poly = [1];
    for (let i = 0; i < degree; i++) {
      const root = EXP[i];
      const next = new Array(poly.length + 1).fill(0);
      for (let j = 0; j < poly.length; j++) {
        next[j] ^= poly[j];
        next[j + 1] ^= gfMul(poly[j], root);
      }
      poly = next;
    }
    return poly;
  };

  const rsEncode = (data: number[], numEc: number) => {
    const gen = rsPoly(numEc);
    let rem = new Array(numEc).fill(0);
    for (const b of data) {
      const factor = b ^ rem[0];
      rem = [...rem.slice(1), 0];
      if (factor !== 0) {
        for (let j = 0; j < numEc; j++) {
          rem[j] ^= gfMul(gen[j + 1], factor);
        }
      }
    }
    return rem;
  };

  const rawBytes = new TextEncoder().encode(text);
  const version = rawBytes.length <= 26 ? 2 : 3;
  const size = version * 4 + 17;
  const totalDataBytes = version === 2 ? 28 : 44;
  const numEc = version === 2 ? 16 : 26;

  // Build bitstream
  const bits: number[] = [0, 1, 0, 0]; // Byte mode
  const countBits = 8;
  for (let i = countBits - 1; i >= 0; i--) bits.push((rawBytes.length >> i) & 1);
  for (const b of rawBytes) {
    for (let i = 7; i >= 0; i--) bits.push((b >> i) & 1);
  }
  const maxBits = totalDataBytes * 8;
  const padZeros = Math.min(4, maxBits - bits.length);
  for (let i = 0; i < padZeros; i++) bits.push(0);
  while (bits.length % 8 !== 0) bits.push(0);

  const dataCodewords: number[] = [];
  for (let i = 0; i < bits.length; i += 8) {
    let cw = 0;
    for (let j = 0; j < 8; j++) cw = (cw << 1) | bits[i + j];
    dataCodewords.push(cw);
  }
  const pad = [0xec, 0x11];
  let pIdx = 0;
  while (dataCodewords.length < totalDataBytes) {
    dataCodewords.push(pad[pIdx]);
    pIdx = (pIdx + 1) % 2;
  }

  const ecCodewords = rsEncode(dataCodewords, numEc);
  const finalStream = [...dataCodewords, ...ecCodewords];

  // Base matrix
  const matrix: (boolean | null)[][] = Array.from({ length: size }, () => new Array(size).fill(null));

  // Finders
  const addFinder = (r: number, c: number) => {
    for (let dr = -1; dr <= 7; dr++) {
      for (let dc = -1; dc <= 7; dc++) {
        const rr = r + dr;
        const cc = c + dc;
        if (rr >= 0 && rr < size && cc >= 0 && cc < size) {
          if (
            (dr >= 0 && dr <= 6 && (dc === 0 || dc === 6)) ||
            (dc >= 0 && dc <= 6 && (dr === 0 || dr === 6)) ||
            (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4)
          ) {
            matrix[rr][cc] = true;
          } else {
            matrix[rr][cc] = false;
          }
        }
      }
    }
  };
  addFinder(0, 0);
  addFinder(0, size - 7);
  addFinder(size - 7, 0);

  // Alignment pattern
  if (version >= 2) {
    const pos = version === 2 ? [6, 18] : [6, 22];
    for (const pr of pos) {
      for (const pc of pos) {
        if (matrix[pr][pc] !== null) continue;
        for (let dr = -2; dr <= 2; dr++) {
          for (let dc = -2; dc <= 2; dc++) {
            matrix[pr + dr][pc + dc] =
              dr === -2 || dr === 2 || dc === -2 || dc === 2 || (dr === 0 && dc === 0);
          }
        }
      }
    }
  }

  // Timing patterns
  for (let i = 8; i < size - 8; i++) {
    if (matrix[i][6] === null) matrix[i][6] = i % 2 === 0;
    if (matrix[6][i] === null) matrix[6][i] = i % 2 === 0;
  }

  // Dark module
  matrix[size - 8][8] = true;

  // Format info (EC Level M = 0, Mask = 0) -> BCH = 0x5412 ^ 0x0000 = 0x5412 (bits)
  // Mask 0 BCH info: 101010000010010
  const formatBits = 0x5412;
  for (let i = 0; i < 15; i++) {
    const bitVal = ((formatBits >> i) & 1) === 1;
    if (i < 6) matrix[i][8] = bitVal;
    else if (i < 8) matrix[i + 1][8] = bitVal;
    else matrix[size - 15 + i][8] = bitVal;

    if (i < 8) matrix[8][size - i - 1] = bitVal;
    else if (i < 9) matrix[8][15 - i] = bitVal;
    else matrix[8][15 - i - 1] = bitVal;
  }

  // Zigzag placement with Mask 0 ((r + c) % 2 === 0)
  let inc = -1;
  let row = size - 1;
  let bitIdx = 7;
  let byteIdx = 0;
  for (let col = size - 1; col > 0; col -= 2) {
    if (col === 6) col--;
    const colRange = [col, col - 1];
    while (true) {
      for (const c of colRange) {
        if (matrix[row][c] === null) {
          let dark = false;
          if (byteIdx < finalStream.length) {
            dark = ((finalStream[byteIdx] >> bitIdx) & 1) === 1;
          }
          if ((row + c) % 2 === 0) dark = !dark;
          matrix[row][c] = dark;
          bitIdx--;
          if (bitIdx === -1) {
            byteIdx++;
            bitIdx = 7;
          }
        }
      }
      row += inc;
      if (row < 0 || size <= row) {
        row -= inc;
        inc = -inc;
        break;
      }
    }
  }

  return matrix.map((r) => r.map((cell) => Boolean(cell)));
}

export function QrCodeView({
  value,
  size = 180,
  quietZone = 4,
}: {
  value: string;
  size?: number;
  quietZone?: number;
}) {
  const matrix = useMemo(() => {
    try {
      return generateQrMatrix(value);
    } catch {
      return [];
    }
  }, [value]);

  if (!matrix.length) return null;

  const moduleCount = matrix.length;
  const totalCount = moduleCount + quietZone * 2;

  let pathData = "";
  for (let r = 0; r < moduleCount; r++) {
    for (let c = 0; c < moduleCount; c++) {
      if (matrix[r][c]) {
        pathData += `M${c + quietZone},${r + quietZone}h1v1h-1z `;
      }
    }
  }

  return (
    <div
      style={{
        display: "inline-block",
        background: "#ffffff",
        padding: 12,
        borderRadius: 8,
        border: "1px solid var(--border)",
        boxShadow: "0 4px 12px rgba(0,0,0,0.06)",
      }}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${totalCount} ${totalCount}`}
        style={{ display: "block", shapeRendering: "crispEdges" }}
        role="img"
        aria-label="QR Code"
      >
        <rect width={totalCount} height={totalCount} fill="#ffffff" />
        <path d={pathData} fill="#000000" />
      </svg>
    </div>
  );
}
