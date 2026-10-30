/**
 * Google Apps Script web app that receives bets from arb_calculator.html and
 * appends them to the active spreadsheet (columns A-P).
 *
 * Setup
 *   1. Bind the script to the spreadsheet (Extensions -> Apps Script).
 *   2. Project Settings -> Script properties -> add ARB_SECRET = <4-digit PIN>.
 *      A short PIN is acceptable only because of the lockout below: after
 *      MAX_FAILS wrong PINs, ALL writes are refused for LOCKOUT_SECONDS.
 *      The secret is NOT in this file; it lives only in the script's properties and,
 *      once, in the browser that uses the calculator (it is typed into a prompt).
 *   3. Deploy -> New deployment -> Web app -> Execute as: me, Who has access: anyone.
 *      Each "New deployment" creates a new /exec URL; to retire a leaked URL, create a
 *      new deployment and archive the old one.
 *
 * The page posts with mode "no-cors", so it cannot read the reply: a wrong key fails
 * silently on the page and shows up only in this script's Executions log.
 */

// Order of the columns A..P in the sheet.
var TEXT_COLUMNS = ['day', 'time', 'homeTeam', 'awayTeam', 's1', 's2', 's3', 'score'];
var NUMBER_COLUMNS = ['totalSum', 'k1', 'b1', 'k2', 'b2', 'k3', 'b3', 'profit'];
var COLUMN_ORDER = [
  'day', 'time', 'homeTeam', 'awayTeam', 'totalSum',   // A-E
  's1', 'k1', 'b1', 's2', 'k2', 'b2', 's3', 'k3', 'b3', // F-N
  'score', 'profit'                                      // O-P
];
var MAX_TEXT_LENGTH = 100;

// Sheet (tab) the rows are appended to.
var SHEET_NAME = 'Лист1';

// Brute-force protection for the short PIN.
var MAX_FAILS = 5;
var LOCKOUT_SECONDS = 3600;
var FAILS_KEY = 'arb_failed_attempts';

function doPost(e) {
  var secret = PropertiesService.getScriptProperties().getProperty('ARB_SECRET');
  if (!secret) return reply_({ ok: false, error: 'not configured' });

  var data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply_({ ok: false, error: 'bad request' });
  }

  // Locked out after too many wrong PINs: refuse everything, even a correct PIN.
  var cache = CacheService.getScriptCache();
  var fails = parseInt(cache.get(FAILS_KEY) || '0', 10);
  if (fails >= MAX_FAILS) {
    return reply_({ ok: false, error: 'locked' });
  }

  // Reject anything that does not carry the shared secret.
  if (!safeEqual_(String(data.key || ''), secret)) {
    cache.put(FAILS_KEY, String(fails + 1), LOCKOUT_SECONDS);
    return reply_({ ok: false, error: 'forbidden' });
  }

  var row = COLUMN_ORDER.map(function (name) {
    return TEXT_COLUMNS.indexOf(name) >= 0 ? cleanText_(data[name]) : cleanNumber_(data[name]);
  });

  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME).appendRow(row);
  } finally {
    lock.releaseLock();
  }
  return reply_({ ok: true });
}

/** Constant-time string comparison, so response timing does not leak the secret. */
function safeEqual_(a, b) {
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Plain text only: truncated, and prefixed so a cell can never start a formula. */
function cleanText_(value) {
  var text = String(value == null ? '' : value).slice(0, MAX_TEXT_LENGTH);
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}

function cleanNumber_(value) {
  var n = parseFloat(value);
  return isFinite(n) ? n : '';
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** Run from the editor (Run -> testDoPost) to check the setup: appends one test row. */
function testDoPost() {
  var secret = PropertiesService.getScriptProperties().getProperty('ARB_SECRET');
  var fakeEvent = { postData: { contents: JSON.stringify({
    key: secret, day: 'TestDay', time: '20:00', homeTeam: 'Test', awayTeam: 'Test2',
    totalSum: '100', s1: 'Bet365', k1: '3', b1: '33', s2: 'Bet365', k2: '4', b2: '25',
    s3: 'Bet365', k3: '5', b3: '20', score: '', profit: '1'
  }) } };
  Logger.log(doPost(fakeEvent).getContent());
}
