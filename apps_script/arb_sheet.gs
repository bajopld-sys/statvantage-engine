/**
 * Google Apps Script web app that receives bets from arb_calculator.html and
 * appends them to the active spreadsheet (columns A-P).
 *
 * Setup
 *   1. Bind the script to the spreadsheet (Extensions -> Apps Script).
 *   2. Project Settings -> Script properties -> add ARB_SECRET = <long random string>.
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

function doPost(e) {
  var secret = PropertiesService.getScriptProperties().getProperty('ARB_SECRET');
  if (!secret) return reply_({ ok: false, error: 'not configured' });

  var data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply_({ ok: false, error: 'bad request' });
  }

  // Reject anything that does not carry the shared secret.
  if (!safeEqual_(String(data.key || ''), secret)) {
    return reply_({ ok: false, error: 'forbidden' });
  }

  var row = COLUMN_ORDER.map(function (name) {
    return TEXT_COLUMNS.indexOf(name) >= 0 ? cleanText_(data[name]) : cleanNumber_(data[name]);
  });

  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    SpreadsheetApp.getActiveSheet().appendRow(row);
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
