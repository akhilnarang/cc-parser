/* storage.js — PasswordCache (in-memory) + StatementStore (IndexedDB).
 *
 * PasswordCache: session-scoped, no persistence. Cleared on tab close.
 * StatementStore: IndexedDB CRUD, PDF hashing, aggregation helpers.
 */

// ── PasswordCache ──────────────────────────────────────────

class PasswordCache {
  constructor() {
    this._cache = new Map();
  }

  /** @param {string} bank */
  get(bank) {
    const entry = this._cache.get(bank);
    return entry ? entry.value : null;
  }

  /** @param {string} bank @param {string} password */
  set(bank, password) {
    this._cache.set(bank, { value: password, fetched_at: new Date().toISOString() });
  }

  clear() {
    this._cache.clear();
  }

  /** @returns {string[]} */
  banks() {
    return Array.from(this._cache.keys());
  }

  /** @param {string} bank */
  delete(bank) {
    this._cache.delete(bank);
  }

  get size() {
    return this._cache.size;
  }
}

// ── StatementStore ─────────────────────────────────────────

const DB_NAME = "ccparser_statements";
const DB_VERSION = 1;
const STORE_NAME = "statements";

class StatementStore {
  constructor() {
    /** @type {IDBDatabase|null} */
    this._db = null;
    this._available = true;
  }

  /** Open IndexedDB, create object store + indexes.
   *  @returns {Promise<boolean>} true if DB is available */
  async init() {
    if (this._db) return true;
    try {
      this._db = await new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = (e) => {
          const db = e.target.result;
          if (!db.objectStoreNames.contains(STORE_NAME)) {
            const store = db.createObjectStore(STORE_NAME, { keyPath: "id" });
            store.createIndex("bank", "bank", { unique: false });
            store.createIndex("card_last_four", "card_last_four", { unique: false });
            store.createIndex("due_date", "due_date", { unique: false });
            store.createIndex("stored_at", "stored_at", { unique: false });
            store.createIndex("semantic_key", "semantic_key", { unique: false });
          }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
      return true;
    } catch (err) {
      console.warn("IndexedDB unavailable:", err);
      this._available = false;
      return false;
    }
  }

  get available() {
    return this._available;
  }

  /** SHA-256 hash of raw PDF bytes → hex string.
   *  @param {ArrayBuffer} arrayBuffer
   *  @returns {Promise<string>} */
  async hashPDF(arrayBuffer) {
    const digest = await crypto.subtle.digest("SHA-256", arrayBuffer);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }

  /** Convert DD/MM/YYYY to ISO YYYY-MM-DD. Returns "unknown" on failure.
   *  @param {string|null|undefined} dateStr
   *  @returns {string} */
  static normalizeDate(dateStr) {
    if (!dateStr) return "unknown";
    // Already ISO?
    if (/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return dateStr;
    // DD/MM/YYYY
    const m = dateStr.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    if (m) return `${m[3]}-${m[2]}-${m[1]}`;
    return "unknown";
  }

  /** Extract last 4 digits of card number.
   *  @param {string|null|undefined} cardNumber
   *  @returns {string} */
  static lastFour(cardNumber) {
    if (!cardNumber) return "unknown";
    const digits = cardNumber.replace(/\D/g, "");
    return digits.length >= 4 ? digits.slice(-4) : "unknown";
  }

  /** Build semantic_key: "bank|last4|due_date_iso".
   *  @param {object} data - parsed statement data
   *  @returns {string} */
  static buildSemanticKey(data) {
    const bank = (data.bank || "unknown").toLowerCase();
    const last4 = StatementStore.lastFour(data.card_number);
    const due = StatementStore.normalizeDate(data.due_date);
    return `${bank}|${last4}|${due}`;
  }

  /** Store a parsed statement with its PDF hash as ID.
   *  @param {object} statementData - parsed statement dict
   *  @param {ArrayBuffer} pdfBuffer - raw PDF bytes
   *  @returns {Promise<object>} the stored record */
  async put(statementData, pdfBuffer) {
    this._ensureDb();
    const id = await this.hashPDF(pdfBuffer);
    const d = statementData;

    const txnCount =
      (d.transactions ? d.transactions.length : 0) +
      (d.payments_refunds ? d.payments_refunds.length : 0);

    const record = {
      id,
      semantic_key: StatementStore.buildSemanticKey(d),
      bank: (d.bank || "unknown").toLowerCase(),
      card_number: d.card_number || "unknown",
      card_last_four: StatementStore.lastFour(d.card_number),
      due_date: StatementStore.normalizeDate(d.due_date),
      name: d.name || "unknown",
      txn_count: txnCount,
      overall_total: d.overall_total || "0.00",
      overall_reward_points: d.overall_reward_points || 0,
      reward_points_balance: d.reward_points_balance || null,
      stored_at: new Date().toISOString(),
      size: JSON.stringify(d).length,
      data: d,
    };

    await this._tx("readwrite", (store) => store.put(record));
    return record;
  }

  /** @param {string} id @returns {Promise<object|undefined>} */
  async get(id) {
    this._ensureDb();
    return this._tx("readonly", (store) => store.get(id));
  }

  /** @returns {Promise<object[]>} */
  async all() {
    this._ensureDb();
    return this._tx("readonly", (store) => store.getAll());
  }

  /** @param {string} id @returns {Promise<void>} */
  async delete(id) {
    this._ensureDb();
    await this._tx("readwrite", (store) => store.delete(id));
  }

  /** @returns {Promise<void>} */
  async deleteAll() {
    this._ensureDb();
    await this._tx("readwrite", (store) => store.clear());
  }

  /** @param {string} bank @returns {Promise<object[]>} */
  async byBank(bank) {
    this._ensureDb();
    return this._tx("readonly", (store) =>
      store.index("bank").getAll(bank.toLowerCase()),
    );
  }

  /** Group statements by due_date month, sum totals.
   *  @param {object[]} [stmts] - pre-fetched records (avoids extra IDB read)
   *  @returns {Promise<Array<{month: string, total: number, count: number}>>} */
  async aggregateByMonth(stmts) {
    if (!stmts) stmts = await this.all();
    const map = new Map();
    for (const s of stmts) {
      const month = s.due_date !== "unknown" ? s.due_date.slice(0, 7) : "unknown";
      const cur = map.get(month) || { month, total: 0, count: 0 };
      cur.total += StatementStore.parseAmount(s.overall_total);
      cur.count += 1;
      map.set(month, cur);
    }
    return Array.from(map.values()).sort((a, b) => a.month.localeCompare(b.month));
  }

  /** Group by (name, card_last_four), sum totals.
   *  @param {object[]} [stmts] - pre-fetched records (avoids extra IDB read)
   *  @returns {Promise<Array<{name: string, card: string, txns: number, total: number}>>} */
  async aggregateByPersonCard(stmts) {
    if (!stmts) stmts = await this.all();
    const map = new Map();
    for (const s of stmts) {
      const summaries = (s.data && s.data.card_summaries) || [];
      for (const cs of summaries) {
        const card = StatementStore.lastFour(cs.card_number);
        const key = `${cs.person || s.name}|${card}`;
        const cur = map.get(key) || { name: cs.person || s.name, card, txns: 0, total: 0 };
        cur.txns += cs.transaction_count || 0;
        cur.total += StatementStore.parseAmount(cs.total_amount);
        map.set(key, cur);
      }
    }
    return Array.from(map.values()).sort((a, b) => b.total - a.total);
  }

  /** Sum all reward points across statements.
   *  @param {object[]} [stmts] - pre-fetched records (avoids extra IDB read)
   *  @returns {Promise<number>} */
  async totalRewardPoints(stmts) {
    if (!stmts) stmts = await this.all();
    return stmts.reduce((sum, s) => {
      const pts = parseFloat(String(s.overall_reward_points || 0).replace(/,/g, ""));
      return sum + (isNaN(pts) ? 0 : pts);
    }, 0);
  }

  /** Get the latest reward points balance (most recent statement).
   *  @param {object[]} [stmts] - pre-fetched records (avoids extra IDB read)
   *  @returns {Promise<number>} */
  async latestRewardPoints(stmts) {
    if (!stmts) stmts = await this.all();
    if (stmts.length === 0) return 0;
    // Find newest by due_date (statement period) without mutating the input array
    const sorted = [...stmts].sort((a, b) => (b.due_date || "").localeCompare(a.due_date || ""));
    // Prefer cumulative balance when available; fall back to earned-this-cycle.
    const raw = sorted[0].reward_points_balance ?? sorted[0].overall_reward_points ?? 0;
    const pts = parseFloat(String(raw).replace(/,/g, ""));
    return isNaN(pts) ? 0 : pts;
  }

  /** Estimate bytes used by all stored statements.
   *  @returns {Promise<{bytes: number, count: number}>} */
  async storageUsage() {
    const stmts = await this.all();
    const bytes = stmts.reduce((sum, s) => sum + (s.size || 0), 0);
    return { bytes, count: stmts.length };
  }

  /** Parse a comma-formatted amount string to a number.
   *  @param {string} amountStr e.g. "3,42,000.00"
   *  @returns {number} */
  static parseAmount(amountStr) {
    if (!amountStr) return 0;
    const n = parseFloat(String(amountStr).replace(/,/g, ""));
    return isNaN(n) ? 0 : n;
  }

  // ── internal helpers ──────────────────────────────────────

  _ensureDb() {
    if (!this._db) throw new Error("StatementStore not initialized. Call init() first.");
  }

  /** Run a single IDB transaction and return the request result.
   *  For reads: resolves with req.result on tx.oncomplete.
   *  For writes: resolves on tx.oncomplete (guarantees commit).
   *  @param {"readonly"|"readwrite"} mode
   *  @param {function(IDBObjectStore): IDBRequest} fn
   *  @returns {Promise<any>} */
  _tx(mode, fn) {
    return new Promise((resolve, reject) => {
      const tx = this._db.transaction(STORE_NAME, mode);
      const store = tx.objectStore(STORE_NAME);
      const req = fn(store);
      let result;
      if (req && typeof req.onsuccess !== "undefined") {
        req.onsuccess = () => { result = req.result; };
        req.onerror = () => reject(req.error);
      }
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("Transaction aborted"));
    });
  }
}

/** Detect if IndexedDB is available (private browsing, old browsers, etc).
 *  @returns {Promise<boolean>} */
async function isIndexedDBAvailable() {
  if (typeof indexedDB === "undefined") return false;
  try {
    const req = indexedDB.open("_ccparser_test", 1);
    return await new Promise((resolve) => {
      req.onsuccess = () => {
        req.result.close();
        indexedDB.deleteDatabase("_ccparser_test");
        resolve(true);
      };
      req.onerror = () => resolve(false);
    });
  } catch {
    return false;
  }
}

/** Detect Mobile Safari for quota warning.
 *  @returns {boolean} */
function isMobileSafari() {
  const ua = navigator.userAgent || "";
  return /Safari/.test(ua) && /iPhone|iPad|iPod/.test(ua) && !/Chrome|CriOS|FxiOS/.test(ua);
}
