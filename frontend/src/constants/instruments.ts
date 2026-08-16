/** Measuring instruments for inspection report rows. */
export const INSTRUMENT_OPTIONS = [
  "Vernier Caliper",
  "Digital Vernier Caliper",
  "Micrometer",
  "Height Gauge",
  "Dial Indicator",
  "Bore Gauge",
  "CMM",
  "Vision Inspection System",
  "Laser Scanner",
  "Measuring Tape",
] as const;

/** Units allowed for each instrument (per shop standard). */
export const UNITS_BY_INSTRUMENT: Record<string, string[]> = {
  "Vernier Caliper": ["mm"],
  "Digital Vernier Caliper": ["mm"],
  Micrometer: ["mm"],
  "Height Gauge": ["mm"],
  "Dial Indicator": ["mm"],
  "Bore Gauge": ["mm"],
  CMM: ["mm"],
  "Vision Inspection System": ["mm"],
  "Laser Scanner": ["mm"],
  "Measuring Tape": ["mm", "cm", "m"],
};

const ALL_UNITS = ["mm", "cm", "m", "in"];

export function unitOptionsForInstrument(instrument: string, currentUnit = ""): string[] {
  const base = instrument
    ? UNITS_BY_INSTRUMENT[instrument] || ["mm"]
    : ALL_UNITS;
  if (currentUnit && !base.includes(currentUnit)) {
    return [currentUnit, ...base];
  }
  return base;
}

export function defaultUnitForInstrument(instrument: string): string {
  const units = UNITS_BY_INSTRUMENT[instrument];
  return units?.[0] || "mm";
}

/** Calibration / asset IDs grouped by instrument type. */
export const INSTRUMENT_IDS_BY_TYPE: Record<string, string[]> = {
  "Vernier Caliper": ["VC-001", "VC-002", "VC-003"],
  "Digital Vernier Caliper": ["DVC-001", "DVC-002", "DVC-003"],
  Micrometer: ["MIC-001", "MIC-002", "MIC-003"],
  "Height Gauge": ["HG-001", "HG-002"],
  "Dial Indicator": ["DI-001", "DI-002"],
  "Bore Gauge": ["BG-001", "BG-002"],
  CMM: ["CMM-001", "CMM-002"],
  "Vision Inspection System": ["VIS-001", "VIS-002"],
  "Laser Scanner": ["LS-001", "LS-002"],
  "Measuring Tape": ["MT-001", "MT-002"],
};

const ID_STORE_KEY = "smorx_instr_ids_v1";

type IdCatalog = Record<string, string[]>;

function loadIdCatalog(): IdCatalog {
  let saved: IdCatalog = {};
  try {
    saved = JSON.parse(localStorage.getItem(ID_STORE_KEY) || "{}") as IdCatalog;
  } catch {
    saved = {};
  }
  const out: IdCatalog = {};
  for (const [k, defaults] of Object.entries(INSTRUMENT_IDS_BY_TYPE)) {
    out[k] = Array.from(new Set([...(defaults || []), ...((saved[k] || []).map((x) => String(x).trim()).filter(Boolean))]));
  }
  for (const [k, ids] of Object.entries(saved)) {
    if (!out[k]) out[k] = (ids || []).map((x) => String(x).trim()).filter(Boolean);
  }
  return out;
}

function saveIdCatalog(cat: IdCatalog) {
  localStorage.setItem(ID_STORE_KEY, JSON.stringify(cat));
}

const ALL_INSTRUMENT_IDS = () =>
  Array.from(new Set(Object.values(loadIdCatalog()).flat())).sort();

export function instrumentIdOptions(instrument: string, currentId = ""): string[] {
  const cat = loadIdCatalog();
  const forType = instrument ? cat[instrument] : undefined;
  const base = forType?.length ? forType : ALL_INSTRUMENT_IDS();
  if (currentId && !base.includes(currentId)) {
    return [currentId, ...base];
  }
  return base;
}

export function addInstrumentId(instrument: string, id: string): string[] {
  const name = id.trim();
  if (!name) return instrumentIdOptions(instrument);
  const cat = loadIdCatalog();
  const key = instrument.trim() || "_custom";
  cat[key] = Array.from(new Set([...(cat[key] || []), name]));
  saveIdCatalog(cat);
  return instrumentIdOptions(instrument, name);
}

export function renameInstrumentId(oldId: string, newId: string, instrument = ""): string {
  const from = oldId.trim();
  const to = newId.trim();
  if (!from || !to || from === to) return from;
  const cat = loadIdCatalog();
  const keys = instrument && cat[instrument] ? [instrument, ...Object.keys(cat)] : Object.keys(cat);
  for (const k of Array.from(new Set(keys))) {
    if (!cat[k]) continue;
    cat[k] = cat[k].map((x) => (x === from ? to : x));
    cat[k] = Array.from(new Set(cat[k]));
  }
  saveIdCatalog(cat);
  return to;
}

export function allInstrumentIds(): string[] {
  return ALL_INSTRUMENT_IDS();
}
