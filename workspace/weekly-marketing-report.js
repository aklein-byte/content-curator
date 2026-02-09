import 'dotenv/config';
import { google } from 'googleapis';
import { readFileSync, writeFileSync, existsSync } from 'fs';
import { pool } from './lib/db.js';
import { postBlocks, buildBlocks } from './lib/slack.js';
import { callClaude } from './lib/claude.js';

const SPREADSHEET_ID = process.env.GOOGLE_SHEETS_ID || '135Pa6HW5ogh8gduJwxTUtEnZZVHo25f4o3L0m2fCZsA';
const CHANNEL = process.env.MARKETING_SLACK_CHANNEL || 'wp-marketing';
const SERVICE_ACCOUNT_PATH = process.env.GOOGLE_SERVICE_ACCOUNT_PATH
  || '/home/aklein_watchungpediatrics_com/hometown-new-dashboard/data/service-account-key.json';
const SNAPSHOT_PATH = process.env.SNAPSHOT_PATH
  || '/home/aklein_watchungpediatrics_com/ehr-api/data/marketing-snapshots.json';

const CLASS_SHEET_1 = process.env.CLASS_SHEET_1 || '12alQPnQydxVbl2DEjxGdtKJPtnC5xJ5yoJI3rnE1qBM'; // New Parent Classes
const CLASS_SHEET_2 = process.env.CLASS_SHEET_2 || '1ZZ2xxqwb2f0-ngriygb4YcXEXPNvGilVQwUkDTwBaNo'; // BSCWI

const FB_USER_TOKEN = process.env.FB_USER_TOKEN;
const FB_PAGE_ID = '137131246387067'; // Watchung Pediatrics
const FB_LEAD_FORM_IDS = [
  '4309237719391484', // Expecting Dads Class Feb 2026
  '1261642312495951', // Untitled form
  '1641160940657872', // Jan 29 form
  '689093660628134',  // Nov 17 form
  '1225115082422180', // Messenger form
  '3139067682963114', // Jan 29 form 2
  '1098163082222992', // Nov 17 form 2
  '1346534386968965', // Nov 6 form
];

const DRY_RUN = process.argv.includes('--dry-run');

// --- Google Sheets ---

async function getSheets() {
  const creds = JSON.parse(readFileSync(SERVICE_ACCOUNT_PATH, 'utf8'));
  const auth = new google.auth.GoogleAuth({
    credentials: creds,
    scopes: ['https://www.googleapis.com/auth/spreadsheets.readonly'],
  });
  return google.sheets({ version: 'v4', auth });
}

async function readSheetTab(sheets, tab, range, spreadsheetId = SPREADSHEET_ID) {
  const res = await sheets.spreadsheets.values.get({
    spreadsheetId,
    range: `${tab}!${range}`,
  });
  return res.data.values || [];
}

// --- Facebook Leads API ---

async function fetchFBLeads() {
  if (!FB_USER_TOKEN) {
    console.log('  No FB_USER_TOKEN set, skipping FB leads');
    return [];
  }

  // Get page access token
  const pageRes = await fetch(
    `https://graph.facebook.com/v21.0/${FB_PAGE_ID}?fields=access_token&access_token=${FB_USER_TOKEN}`
  );
  const pageData = await pageRes.json();
  if (pageData.error) {
    console.error('  FB page token error:', pageData.error.message);
    return [];
  }
  const pageToken = pageData.access_token;

  const allLeads = [];
  for (const formId of FB_LEAD_FORM_IDS) {
    try {
      const res = await fetch(
        `https://graph.facebook.com/v21.0/${formId}/leads?fields=created_time,field_data&limit=500&access_token=${pageToken}`
      );
      const data = await res.json();
      if (data.error) continue;
      for (const lead of (data.data || [])) {
        const fields = {};
        for (const f of (lead.field_data || [])) {
          fields[f.name.toLowerCase()] = f.values?.[0] || '';
        }
        const phone = normalizePhone(fields.phone_number || fields.phone || '');
        const email = (fields.email || '').trim().toLowerCase();
        allLeads.push({
          formId,
          date: lead.created_time?.slice(0, 10) || '',
          phone,
          email,
          source: 'facebook_ad',
        });
      }
    } catch (e) {
      console.error(`  FB form ${formId} error:`, e.message);
    }
  }
  return allLeads;
}

// --- Class RSVP Sheets ---

const CLASS_TABS = [
  // Sheet 1: New Parent Classes
  { sheetId: 'CLASS_SHEET_1', tab: 'Form Responses 1', label: 'New Parent Class Form' },
  { sheetId: 'CLASS_SHEET_1', tab: 'Scott 2/4 Class', label: 'Scott 2/4 Class' },
  { sheetId: 'CLASS_SHEET_1', tab: 'De Los Rios 2/18 Class', label: 'De Los Rios 2/18 Class' },
  // Sheet 2: BSCWI
  { sheetId: 'CLASS_SHEET_2', tab: 'Form Responses 1', label: 'BSCWI Form' },
  { sheetId: 'CLASS_SHEET_2', tab: 'LEADS + FORM NOV', label: 'BSCWI Nov' },
  { sheetId: 'CLASS_SHEET_2', tab: 'LEADS + FORM DEC', label: 'BSCWI Dec' },
  { sheetId: 'CLASS_SHEET_2', tab: 'JANUARY', label: 'BSCWI Jan' },
  { sheetId: 'CLASS_SHEET_2', tab: 'FEBRUARY', label: 'BSCWI Feb' },
  { sheetId: 'CLASS_SHEET_2', tab: 'MARCH', label: 'BSCWI Mar' },
];

function resolveSheetId(key) {
  if (key === 'CLASS_SHEET_1') return CLASS_SHEET_1;
  if (key === 'CLASS_SHEET_2') return CLASS_SHEET_2;
  return key;
}

function findColumnIndex(headers, patterns) {
  const lower = headers.map(h => (h || '').toLowerCase().trim());
  for (const pat of patterns) {
    const idx = lower.findIndex(h => h.includes(pat));
    if (idx >= 0) return idx;
  }
  return -1;
}

async function fetchAllClassRSVPs(sheets) {
  const allRSVPs = [];

  for (const tabDef of CLASS_TABS) {
    const spreadsheetId = resolveSheetId(tabDef.sheetId);
    let rows;
    try {
      rows = await readSheetTab(sheets, tabDef.tab, 'A1:Z100', spreadsheetId);
    } catch (e) {
      console.log(`  Skipping class tab "${tabDef.tab}": ${e.message}`);
      continue;
    }
    if (!rows || rows.length < 2) continue;

    const headers = rows[0];
    const nameCol = findColumnIndex(headers, ['full name', 'your name', 'name']);
    const phoneCol = findColumnIndex(headers, ['phone number', 'phone']);
    const emailCol = findColumnIndex(headers, ['your email', 'email address', 'email']);
    const dueDateCol = findColumnIndex(headers, ['due date', 'arrival date']);
    const referralCol = findColumnIndex(headers, ['ad type', 'how did you hear', 'how they heard', 'where did you hear', 'where they heard', 'location of signup']);
    const classCol = findColumnIndex(headers, ['which class', 'class']);
    const timestampCol = findColumnIndex(headers, ['timestamp', 'time stamp', 'date']);

    for (let i = 1; i < rows.length; i++) {
      const row = rows[i];
      if (!row || row.length < 2) continue;

      const name = nameCol >= 0 ? (row[nameCol] || '').trim() : '';
      const phone = normalizePhone(phoneCol >= 0 ? (row[phoneCol] || '') : '');
      const email = emailCol >= 0 ? (row[emailCol] || '').trim().toLowerCase() : '';
      const dueDate = dueDateCol >= 0 ? (row[dueDateCol] || '').trim() : '';
      const referralRaw = referralCol >= 0 ? (row[referralCol] || '').trim() : '';
      const className = classCol >= 0 ? (row[classCol] || '').trim() : '';
      const timestamp = timestampCol >= 0 ? (row[timestampCol] || '').trim() : '';

      if (!name && !phone && !email) continue;

      allRSVPs.push({
        name,
        phone,
        email,
        dueDate,
        referralRaw,
        referralSource: classifyReferralSource(referralRaw),
        className: className || tabDef.label,
        sheetTab: tabDef.label,
        timestamp,
      });
    }
  }

  // Dedup by phone (keep first occurrence, merge class names)
  const byPhone = new Map();
  const byEmail = new Map();
  const deduped = [];

  for (const rsvp of allRSVPs) {
    const key = rsvp.phone && rsvp.phone.length === 10 ? rsvp.phone : null;
    const emailKey = rsvp.email || null;

    let existing = null;
    if (key && byPhone.has(key)) existing = byPhone.get(key);
    else if (emailKey && byEmail.has(emailKey)) existing = byEmail.get(emailKey);

    if (existing) {
      if (!existing.classesAttended.includes(rsvp.sheetTab)) {
        existing.classesAttended.push(rsvp.sheetTab);
      }
      // Keep the more specific referral
      if (existing.referralSource === 'unknown' && rsvp.referralSource !== 'unknown') {
        existing.referralSource = rsvp.referralSource;
        existing.referralRaw = rsvp.referralRaw;
      }
    } else {
      const entry = { ...rsvp, classesAttended: [rsvp.sheetTab] };
      deduped.push(entry);
      if (key) byPhone.set(key, entry);
      if (emailKey) byEmail.set(emailKey, entry);
    }
  }

  return { all: allRSVPs, unique: deduped };
}

// --- Attribution classification ---

function classifyReferralSource(text) {
  if (!text || !text.trim()) return 'unknown';
  const t = text.trim().toLowerCase();

  // Facebook / Meta ads
  if (t === 'meta' || t.includes('feeding fund') || t.includes('are you expecting')
    || t === 'leads' || t.includes('facebook') || t.includes('fb ad') || t.includes('fb ')) {
    return 'facebook_ad';
  }

  // Instagram
  if (t.includes('instagram') || t === 'ig ad' || t === 'ig') return 'instagram';

  // Employee referral
  if (t.includes('employee referral') || t.includes('marina hanna')
    || t.includes('staff') || t.includes('employee')) return 'employee_referral';

  // Google search
  if (t.includes('google') || t.includes('googling')) return 'google_search';

  // Email marketing
  if (t.includes('list serv') || t.includes('listserv') || t.includes('via email')
    || t === 'email' || t.includes('email blast')) return 'email_marketing';

  // Word of mouth
  if (t.includes('a friend') || t.includes('existing patient') || t.includes('referral')
    || t.includes('word of mouth') || t.includes('friend')) return 'word_of_mouth';

  // Website
  if (t === 'form' || t.includes('your website') || t.includes('website')
    || t.includes('watchung pediatrics') || t.includes('watchung peds')) return 'website';

  return 'unknown';
}

// --- Phone normalization ---

function normalizePhone(raw) {
  if (!raw) return '';
  const digits = raw.replace(/[^0-9]/g, '');
  return digits.length >= 10 ? digits.slice(-10) : digits;
}

function extractLastName(fullName) {
  if (!fullName) return '';
  // Handle compound names: "Tina Kang and Kunalajit (Kunal) Kang" → "kang"
  // Strip parenthetical nicknames first
  let cleaned = fullName.replace(/\([^)]*\)/g, '').trim();
  // If "and" separates two people, take the last word (shared last name)
  const parts = cleaned.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '';
  return parts[parts.length - 1].toLowerCase();
}

function splitEmails(emailField) {
  if (!emailField) return [];
  return emailField.split(/[;,]/).map(e => e.trim().toLowerCase()).filter(e => e.length > 3 && e.includes('@'));
}

function parseDateLoose(str) {
  if (!str) return null;
  const parts = str.split('/');
  if (parts.length !== 3) return null;
  let [m, d, y] = parts.map(Number);
  if (y < 100) y += 2000;
  const date = new Date(y, m - 1, d);
  return isNaN(date.getTime()) ? null : date;
}

// --- Parse Form Responses (the main registration form) ---

function classifyFormStatus(status) {
  if (!status || !status.trim()) return 'not_contacted';
  const s = status.trim().toLowerCase();

  if (s.includes('scheduled') || s.includes('sch ') || s.startsWith('npi scheduled'))
    return 'scheduled';
  if (s.includes('declined') || s.includes('not welcomed') || s.includes('discharged'))
    return 'declined';
  if (s.includes('unable to') || s.includes('numerous attempt'))
    return 'unable_to_reach';
  if (s.includes('lmtcb') || s.includes('l/m') || s.includes('email sent'))
    return 'left_message';
  if (s.includes('pending mr') || s.includes('pending np') || s.includes('pending packet')
    || s.includes('pending ins') || s.includes('pending accom'))
    return 'pending_docs';
  if (s.includes('np process complete') || s.includes('ok to schedule') || s.includes('ok to sch'))
    return 'ready_to_schedule';
  if (s.includes('due for pe') || s.includes('overdue') || s.startsWith('prn'))
    return 'due_for_pe';
  if (s.includes('spoke w/') || s.includes('returned call') || s.includes('mom called')
    || s.includes('dad came') || s.includes('father came'))
    return 'contacted';
  if (s.includes('nicu') || s.includes('out of the country'))
    return 'on_hold';
  if (s.includes('test')) return 'test';

  // Date-like entries (staff notes starting with dates)
  if (/^\d{2}\/\d{2}\/\d{2}/.test(s)) return 'in_progress';

  return 'other';
}

function parseFormResponses(rows) {
  const submissions = [];
  for (let i = 1; i < rows.length; i++) {
    const row = rows[i];
    if (!row || row.length < 4) continue;

    const type = (row[5] || '').trim().toLowerCase();
    const isExpecting = type.includes('expecting');

    submissions.push({
      timestamp: row[0] || '',
      name: row[1] || '',
      email: (row[2] || '').trim().toLowerCase(),
      phone: normalizePhone(row[3]),
      office: (row[7] || '').trim(),
      isExpecting,
      isRegistering: !isExpecting,
      dueDate: row[9] || '',
      status: classifyFormStatus(row[24] || ''), // col Y = Status
      rawStatus: (row[24] || '').trim(),
      aptDate: (row[25] || '').trim(), // col Z = Appointment Scheduled Date
    });
  }
  return submissions;
}

// --- Parse Expecting Parents tab ---

function classifyExpectingLead(npiScheduled, aptScheduled) {
  const npi = (npiScheduled || '').trim();
  const apt = (aptScheduled || '').trim();

  // Check NPI Scheduled column first
  if (npi.toLowerCase() === 'declined') return 'declined';
  if (npi.includes('/') || /^\d/.test(npi)) {
    const d = parseDateLoose(npi);
    if (d) return d > new Date() ? 'scheduled_upcoming' : 'scheduled_past';
  }
  if (npi.toLowerCase().includes('email sent') || npi.toLowerCase().includes('pending'))
    return 'pending_response';
  if (npi.toLowerCase() === 'no') return 'left_message';

  // NPI is blank — check Apt Scheduled column as secondary signal
  if (!npi && apt) {
    const aptLower = apt.toLowerCase();
    if (aptLower === 'not scheduled') return 'contacted'; // they were reached but not scheduled
    // If col H has a date, they're scheduled even though NPI wasn't filled in
    if (apt.includes('/') || /^\d/.test(apt)) {
      const d = parseDateLoose(apt.split('-')[0].trim()); // "01/20/2026- WRN" → parse date part
      if (d) return d > new Date() ? 'scheduled_upcoming' : 'scheduled_past';
    }
  }

  if (!npi && !apt) return 'not_contacted';
  if (npi) return 'other';
  return 'not_contacted';
}

function parseExpectingParents(rows) {
  const leads = [];
  for (let i = 1; i < rows.length; i++) {
    const row = rows[i];
    if (!row || row.length < 2) continue;
    if (row.length <= 2 && !row[3]) continue;
    const timestamp = row[0] || '';
    const name = row[1] || '';
    if (!name || !timestamp.includes('/')) continue;

    leads.push({
      timestamp,
      name,
      email: (row[2] || '').trim().toLowerCase(),
      phone: normalizePhone(row[3]),
      office: (row[4] || '').trim(),
      dueDate: row[5] || '',
      npiScheduled: (row[6] || '').trim(),
      aptScheduled: (row[7] || '').trim(),
      stage: classifyExpectingLead(row[6], row[7]),
    });
  }
  return leads;
}

// --- Funnel analysis ---

// Stage progression order (current state implies all prior stages completed):
//   not_contacted → left_message → contacted/in_progress → pending_docs → ready_to_schedule → scheduled/due_for_pe
// Exit states (drop-off): declined, unable_to_reach, on_hold

// Map each status to a numeric progression level
const STAGE_LEVEL = {
  not_contacted: 0,
  left_message: 1,
  contacted: 2,
  in_progress: 2,
  other: 2,
  pending_docs: 3,
  ready_to_schedule: 4,
  scheduled: 5,
  due_for_pe: 5,
  // Exit states — these people were at least contacted (level 2+)
  declined: -1,
  unable_to_reach: -2,
  on_hold: -3,
};

function analyzeRegistrationFunnel(submissions) {
  const active = submissions.filter(s => s.status !== 'test');
  const total = active.length;

  // Current-state counts (flat view)
  const currentCounts = {};
  for (const sub of active) {
    currentCounts[sub.status] = (currentCounts[sub.status] || 0) + 1;
  }

  // Exit counts
  const declined = currentCounts.declined || 0;
  const unableToReach = currentCounts.unable_to_reach || 0;
  const onHold = currentCounts.on_hold || 0;
  const totalExits = declined + unableToReach + onHold;

  // Cumulative funnel: count everyone who has reached AT LEAST this stage
  // "declined" people were contacted (level 2), "unable_to_reach" were attempted (level 1)
  function countAtLevel(minLevel) {
    return active.filter(s => {
      const level = STAGE_LEVEL[s.status];
      if (level === undefined) return false;
      if (level >= 0) return level >= minLevel;
      // Exit states: declined people made it to at least "contacted" (2)
      // unable_to_reach made it to at least "left_message" (1)
      // on_hold made it to at least "contacted" (2)
      if (s.status === 'declined') return minLevel <= 2;
      if (s.status === 'unable_to_reach') return minLevel <= 1;
      if (s.status === 'on_hold') return minLevel <= 2;
      return false;
    }).length;
  }

  const cumulativeFunnel = [
    { stage: 'Submitted', count: total },
    { stage: 'Contact Attempted', count: countAtLevel(1) },
    { stage: 'Connected', count: countAtLevel(2) },
    { stage: 'Docs Received', count: countAtLevel(3) },
    { stage: 'Ready to Schedule', count: countAtLevel(4) },
    { stage: 'Scheduled', count: countAtLevel(5) },
  ];

  // Add conversion rates between adjacent stages
  for (let i = 1; i < cumulativeFunnel.length; i++) {
    const prev = cumulativeFunnel[i - 1].count;
    const curr = cumulativeFunnel[i].count;
    cumulativeFunnel[i].pctOfPrev = prev > 0 ? Math.round((curr / prev) * 100) : 0;
    cumulativeFunnel[i].pctOfTotal = total > 0 ? Math.round((curr / total) * 100) : 0;
  }
  cumulativeFunnel[0].pctOfTotal = 100;

  // By office — cumulative funnel per office
  const byOffice = {};
  for (const sub of active) {
    const o = sub.office || 'Unknown';
    if (!byOffice[o]) byOffice[o] = { total: 0, contactAttempted: 0, connected: 0, docsReceived: 0, scheduled: 0 };
    byOffice[o].total++;
    const level = STAGE_LEVEL[sub.status];
    const effectiveLevel = level >= 0 ? level
      : sub.status === 'declined' ? 2
      : sub.status === 'unable_to_reach' ? 1
      : sub.status === 'on_hold' ? 2 : 0;
    if (effectiveLevel >= 1) byOffice[o].contactAttempted++;
    if (effectiveLevel >= 2) byOffice[o].connected++;
    if (effectiveLevel >= 3) byOffice[o].docsReceived++;
    if (effectiveLevel >= 5) byOffice[o].scheduled++;
  }
  // Add conversion rates per office
  for (const o of Object.keys(byOffice)) {
    const d = byOffice[o];
    d.contactRate = d.total > 0 ? Math.round((d.contactAttempted / d.total) * 100) : 0;
    d.scheduledRate = d.total > 0 ? Math.round((d.scheduled / d.total) * 100) : 0;
  }

  // Stuck analysis
  const now = new Date();
  const stuckNotContacted = active.filter(s => {
    if (s.status !== 'not_contacted') return false;
    const d = new Date(s.timestamp);
    return !isNaN(d.getTime()) && (now - d) > 14 * 24 * 60 * 60 * 1000;
  }).length;
  const stuckLeftMessage = active.filter(s => {
    if (s.status !== 'left_message') return false;
    const d = new Date(s.timestamp);
    return !isNaN(d.getTime()) && (now - d) > 21 * 24 * 60 * 60 * 1000;
  }).length;
  const stuckPendingDocs = active.filter(s => {
    if (s.status !== 'pending_docs') return false;
    const d = new Date(s.timestamp);
    return !isNaN(d.getTime()) && (now - d) > 30 * 24 * 60 * 60 * 1000;
  }).length;

  // Biggest drop-off (lowest stage-to-stage conversion)
  let biggestDrop = null;
  let lowestRate = 100;
  for (let i = 1; i < cumulativeFunnel.length; i++) {
    if (cumulativeFunnel[i].pctOfPrev < lowestRate) {
      lowestRate = cumulativeFunnel[i].pctOfPrev;
      biggestDrop = {
        from: cumulativeFunnel[i - 1].stage,
        to: cumulativeFunnel[i].stage,
        rate: cumulativeFunnel[i].pctOfPrev,
        lost: cumulativeFunnel[i - 1].count - cumulativeFunnel[i].count,
      };
    }
  }

  return {
    total,
    cumulativeFunnel,
    currentCounts,
    exits: { declined, unableToReach, onHold, total: totalExits },
    stuck: { notContacted14d: stuckNotContacted, leftMessage21d: stuckLeftMessage, pendingDocs30d: stuckPendingDocs },
    biggestDrop,
    byOffice,
  };
}

function analyzeExpectingFunnel(leads) {
  const now = new Date();
  const total = leads.length;

  // Current-state counts with avg days
  const stageCounts = {};
  for (const lead of leads) {
    if (!stageCounts[lead.stage]) stageCounts[lead.stage] = { count: 0, totalDays: 0, withDays: 0 };
    stageCounts[lead.stage].count++;
    const submitted = parseDateLoose(lead.timestamp);
    if (submitted) {
      const days = Math.floor((now - submitted) / (1000 * 60 * 60 * 24));
      stageCounts[lead.stage].totalDays += days;
      stageCounts[lead.stage].withDays++;
    }
  }
  for (const s of Object.values(stageCounts)) {
    s.avgDays = s.withDays > 0 ? Math.round(s.totalDays / s.withDays) : null;
    delete s.totalDays;
    delete s.withDays;
  }

  // Expecting parents stage levels
  const EXP_LEVEL = {
    not_contacted: 0,
    left_message: 1,
    pending_response: 2,
    scheduled_upcoming: 3,
    scheduled_past: 4,
    declined: -1,
    other: 1,
  };

  function countExpAtLevel(minLevel) {
    return leads.filter(l => {
      const level = EXP_LEVEL[l.stage];
      if (level === undefined) return false;
      if (level >= 0) return level >= minLevel;
      if (l.stage === 'declined') return minLevel <= 1;
      return false;
    }).length;
  }

  const cumulativeFunnel = [
    { stage: 'On Tracker', count: total },
    { stage: 'Contacted', count: countExpAtLevel(1) },
    { stage: 'Responded', count: countExpAtLevel(2) },
    { stage: 'NPI Scheduled', count: countExpAtLevel(3) },
    { stage: 'Visit Completed', count: countExpAtLevel(4) },
  ];

  for (let i = 1; i < cumulativeFunnel.length; i++) {
    const prev = cumulativeFunnel[i - 1].count;
    const curr = cumulativeFunnel[i].count;
    cumulativeFunnel[i].pctOfPrev = prev > 0 ? Math.round((curr / prev) * 100) : 0;
    cumulativeFunnel[i].pctOfTotal = total > 0 ? Math.round((curr / total) * 100) : 0;
  }
  cumulativeFunnel[0].pctOfTotal = 100;

  // Stuck count
  const stuckCount = leads.filter(l => {
    const submitted = parseDateLoose(l.timestamp);
    if (!submitted) return false;
    const days = Math.floor((now - submitted) / (1000 * 60 * 60 * 24));
    return (l.stage === 'not_contacted' && days > 14) || (l.stage === 'left_message' && days > 21);
  }).length;

  // Upcoming due dates — only count those NOT already scheduled
  const twoWeeks = new Date(now.getTime() + 14 * 24 * 60 * 60 * 1000);
  const upcomingDueDates = leads.filter(l => {
    const due = parseDateLoose(l.dueDate);
    if (!due || due < now || due > twoWeeks) return false;
    return !l.stage.startsWith('scheduled'); // exclude already-scheduled
  }).length;
  const upcomingDueDatesAll = leads.filter(l => {
    const due = parseDateLoose(l.dueDate);
    return due && due >= now && due <= twoWeeks;
  }).length;
  // Past due dates (baby likely already born, not yet scheduled)
  const pastDueDatesNotScheduled = leads.filter(l => {
    const due = parseDateLoose(l.dueDate);
    return due && due < now && !l.stage.startsWith('scheduled') && l.stage !== 'declined';
  }).length;

  // By office
  const byOffice = {};
  for (const lead of leads) {
    const o = lead.office || 'Unknown';
    if (!byOffice[o]) byOffice[o] = { total: 0, scheduled: 0, notContacted: 0 };
    byOffice[o].total++;
    if (lead.stage === 'scheduled_upcoming' || lead.stage === 'scheduled_past') byOffice[o].scheduled++;
    if (lead.stage === 'not_contacted') byOffice[o].notContacted++;
  }

  // Biggest drop
  let biggestDrop = null;
  let lowestRate = 100;
  for (let i = 1; i < cumulativeFunnel.length; i++) {
    if (cumulativeFunnel[i].pctOfPrev < lowestRate) {
      lowestRate = cumulativeFunnel[i].pctOfPrev;
      biggestDrop = {
        from: cumulativeFunnel[i - 1].stage,
        to: cumulativeFunnel[i].stage,
        rate: cumulativeFunnel[i].pctOfPrev,
      };
    }
  }

  return {
    totalLeads: total,
    cumulativeFunnel,
    stageCounts,
    stuckCount,
    biggestDrop,
    byOffice,
    upcomingDueDatesIn2Weeks: upcomingDueDates,
    upcomingDueDatesIn2WeeksAll: upcomingDueDatesAll,
    pastDueDatesNotScheduled,
  };
}

// --- Submission counts ---

function countSubmissionsThisWeek(submissions) {
  const weekAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
  const results = { total: 0, expecting: 0, registering: 0 };
  for (const s of submissions) {
    const d = new Date(s.timestamp);
    if (!isNaN(d.getTime()) && d >= weekAgo) {
      results.total++;
      if (s.isExpecting) results.expecting++;
      else results.registering++;
    }
  }
  return results;
}

function weeklySubmissionTrend(submissions) {
  const now = new Date();
  const weeks = [];
  for (let w = 0; w < 8; w++) {
    const weekEnd = new Date(now.getTime() - w * 7 * 24 * 60 * 60 * 1000);
    const weekStart = new Date(weekEnd.getTime() - 7 * 24 * 60 * 60 * 1000);
    let total = 0, expecting = 0, registering = 0;
    for (const s of submissions) {
      const d = new Date(s.timestamp);
      if (!isNaN(d.getTime()) && d >= weekStart && d < weekEnd) {
        total++;
        if (s.isExpecting) expecting++;
        else registering++;
      }
    }
    weeks.push({ weekStart: weekStart.toISOString().slice(0, 10), total, expecting, registering });
  }
  return weeks.reverse();
}

// --- EHR Queries ---

const QUERIES = {
  newPatientsThisWeek: `
    WITH first_visit AS (
      SELECT patient_id, MIN(appt_when) as first_appt
      FROM appointment
      WHERE deleted = false
      GROUP BY patient_id
    )
    SELECT
      vl.name as office,
      COUNT(*) as new_patients,
      COUNT(CASE WHEN AGE(fv.first_appt::date, p.dateofbirth) < INTERVAL '3 months' THEN 1 END) as newborns,
      COUNT(CASE WHEN AGE(fv.first_appt::date, p.dateofbirth) >= INTERVAL '3 months' THEN 1 END) as transfers
    FROM first_visit fv
    JOIN appointment a ON fv.patient_id = a.patient_id AND fv.first_appt = a.appt_when
    JOIN patient p ON fv.patient_id = p.id
    JOIN vlocation vl ON a.location_id = vl.id
    WHERE fv.first_appt::date >= CURRENT_DATE - INTERVAL '7 days'
      AND fv.first_appt::date < CURRENT_DATE
      AND p.deceased = false
      AND (p.merged_to_id IS NULL OR p.merged_to_id = 0)
    GROUP BY vl.name
  `,

  weeklyTrends: `
    WITH first_visit AS (
      SELECT patient_id, MIN(appt_when) as first_appt
      FROM appointment
      WHERE deleted = false
      GROUP BY patient_id
    )
    SELECT
      DATE_TRUNC('week', fv.first_appt)::date as week_start,
      vl.name as office,
      COUNT(*) as new_patients,
      COUNT(CASE WHEN AGE(fv.first_appt::date, p.dateofbirth) < INTERVAL '3 months' THEN 1 END) as newborns,
      COUNT(CASE WHEN AGE(fv.first_appt::date, p.dateofbirth) >= INTERVAL '3 months' THEN 1 END) as transfers
    FROM first_visit fv
    JOIN appointment a ON fv.patient_id = a.patient_id AND fv.first_appt = a.appt_when
    JOIN patient p ON fv.patient_id = p.id
    JOIN vlocation vl ON a.location_id = vl.id
    WHERE fv.first_appt >= CURRENT_DATE - INTERVAL '8 weeks'
      AND fv.first_appt < CURRENT_DATE
      AND p.deceased = false
      AND (p.merged_to_id IS NULL OR p.merged_to_id = 0)
    GROUP BY DATE_TRUNC('week', fv.first_appt), vl.name
    ORDER BY week_start, office
  `,

  newPatientsForMatching: `
    WITH first_visit AS (
      SELECT patient_id, MIN(appt_when) as first_appt
      FROM appointment
      WHERE deleted = false
      GROUP BY patient_id
    )
    SELECT
      fv.patient_id,
      vl.name as office,
      fv.first_appt::date as first_visit_date,
      AGE(fv.first_appt::date, p.dateofbirth) < INTERVAL '3 months' as is_newborn,
      REGEXP_REPLACE(COALESCE(acct.xphone1, ''), '[^0-9]', '', 'g') as phone1,
      REGEXP_REPLACE(COALESCE(acct.xphone2, ''), '[^0-9]', '', 'g') as phone2,
      REGEXP_REPLACE(COALESCE(acct.xphone3, ''), '[^0-9]', '', 'g') as phone3,
      REGEXP_REPLACE(COALESCE(acct.xphone4, ''), '[^0-9]', '', 'g') as phone4,
      REGEXP_REPLACE(COALESCE(c.phone, ''), '[^0-9]', '', 'g') as contact_phone,
      LOWER(TRIM(COALESCE(c.email, ''))) as contact_email,
      LOWER(TRIM(COALESCE(c.name_last, ''))) as contact_last_name,
      LOWER(TRIM(COALESCE(c.name_first, ''))) as contact_first_name,
      LOWER(TRIM(COALESCE(p.lastname, ''))) as patient_last_name,
      LOWER(TRIM(COALESCE(p.firstname, ''))) as patient_first_name
    FROM first_visit fv
    JOIN patient p ON fv.patient_id = p.id
    JOIN appointment a ON fv.patient_id = a.patient_id AND fv.first_appt = a.appt_when
    JOIN vlocation vl ON a.location_id = vl.id
    LEFT JOIN account acct ON p.guarantoraccount_id = acct.id
    LEFT JOIN contact c ON acct.contact_id = c.id
    WHERE fv.first_appt >= CURRENT_DATE - INTERVAL '90 days'
      AND fv.first_appt < CURRENT_DATE
      AND p.deceased = false
      AND (p.merged_to_id IS NULL OR p.merged_to_id = 0)
  `,

  warrenNewborns: `
    WITH newborn_visits AS (
      SELECT
        a.patient_id,
        a.appt_when,
        vl.name as office,
        p.dateofbirth,
        ROW_NUMBER() OVER (PARTITION BY a.patient_id ORDER BY a.appt_when) as visit_number
      FROM appointment a
      JOIN patient p ON a.patient_id = p.id
      JOIN vlocation vl ON a.location_id = vl.id
      WHERE a.appt_when >= CURRENT_DATE - INTERVAL '4 weeks'
        AND a.appt_when < CURRENT_DATE
        AND AGE(a.appt_when::date, p.dateofbirth) < INTERVAL '3 months'
        AND a.deleted = false
        AND p.deceased = false
        AND (p.merged_to_id IS NULL OR p.merged_to_id = 0)
    )
    SELECT
      DATE_TRUNC('week', appt_when)::date as week_start,
      office,
      COUNT(DISTINCT patient_id) as unique_newborns
    FROM newborn_visits
    WHERE visit_number = 1
    GROUP BY DATE_TRUNC('week', appt_when), office
    ORDER BY week_start, office
  `,
};

// --- Unified lead model ---

const ATTRIBUTION_PRIORITY = [
  'facebook_ad', 'instagram', 'google_search', 'email_marketing',
  'employee_referral', 'word_of_mouth', 'website', 'class_only', 'unknown',
];

function buildUnifiedLeadList(fbLeads, classRSVPs, formResponses, expectingLeads) {
  // Phone-keyed map, email secondary index
  const byPhone = new Map();
  const byEmail = new Map();
  const leads = [];

  function getOrCreate(phone, email, name) {
    const normPhone = phone && phone.length === 10 ? phone : null;
    const normEmail = email || null;

    let existing = null;
    if (normPhone && byPhone.has(normPhone)) existing = byPhone.get(normPhone);
    else if (normEmail && byEmail.has(normEmail)) existing = byEmail.get(normEmail);

    if (existing) return existing;

    const entry = {
      phone: normPhone || '',
      email: normEmail || '',
      name: name || '',
      sources: [],
      attribution: 'unknown',
      referralDetail: '',
      firstSeen: null,
      office: '',
      classesAttended: [],
      registrationStatus: '',
      expectingStage: '',
    };
    leads.push(entry);
    if (normPhone) byPhone.set(normPhone, entry);
    if (normEmail) byEmail.set(normEmail, entry);
    return entry;
  }

  function updateAttribution(entry, source, referralDetail) {
    if (!entry.sources.includes(source)) entry.sources.push(source);
    const currentPri = ATTRIBUTION_PRIORITY.indexOf(entry.attribution);
    const newPri = ATTRIBUTION_PRIORITY.indexOf(source);
    if (newPri >= 0 && (currentPri < 0 || newPri < currentPri)) {
      entry.attribution = source;
      if (referralDetail) entry.referralDetail = referralDetail;
    }
  }

  function updateFirstSeen(entry, dateStr) {
    if (!dateStr) return;
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return;
    if (!entry.firstSeen || d < entry.firstSeen) entry.firstSeen = d;
  }

  // 1. Facebook leads (highest priority attribution)
  for (const fb of fbLeads) {
    const entry = getOrCreate(fb.phone, fb.email, '');
    updateAttribution(entry, 'facebook_ad', 'Facebook Lead Ad');
    updateFirstSeen(entry, fb.date);
  }

  // 2. Class RSVPs
  for (const rsvp of classRSVPs.unique) {
    const entry = getOrCreate(rsvp.phone, rsvp.email, rsvp.name);
    if (!entry.name && rsvp.name) entry.name = rsvp.name;

    // Use the classified referral source from class data
    const classAttribution = rsvp.referralSource !== 'unknown' ? rsvp.referralSource : 'class_only';
    updateAttribution(entry, classAttribution, rsvp.referralRaw);
    updateFirstSeen(entry, rsvp.timestamp);

    for (const cls of rsvp.classesAttended) {
      if (!entry.classesAttended.includes(cls)) entry.classesAttended.push(cls);
    }
  }

  // 3. Form responses (registration form)
  for (const form of formResponses) {
    const entry = getOrCreate(form.phone, form.email, form.name);
    if (!entry.name && form.name) entry.name = form.name;
    if (!entry.sources.includes('registration_form')) entry.sources.push('registration_form');
    // Only set attribution to 'website' if nothing better is already set
    updateAttribution(entry, 'website', 'Registration Form');
    updateFirstSeen(entry, form.timestamp);
    if (form.office && !entry.office) entry.office = form.office;
    entry.registrationStatus = form.status;
    entry.registrationTimestamp = form.timestamp;
  }

  // 4. Expecting parents
  for (const exp of expectingLeads) {
    const entry = getOrCreate(exp.phone, exp.email, exp.name);
    if (!entry.name && exp.name) entry.name = exp.name;
    if (!entry.sources.includes('expecting_tracker')) entry.sources.push('expecting_tracker');
    // Don't override attribution — expecting tracker is not a source channel
    updateFirstSeen(entry, exp.timestamp);
    if (exp.office && !entry.office) entry.office = exp.office;
    entry.expectingStage = exp.stage;
  }

  return leads;
}

// --- Unified matching ---

function matchLeadsToEHR(unifiedLeads, ehrPatients) {
  // Build EHR indices
  const phoneIndex = new Map();
  const emailIndex = new Map();
  const nameIndex = new Map(); // last name → patients (fallback matching)
  for (const p of ehrPatients) {
    const phones = [p.phone1, p.phone2, p.phone3, p.phone4, p.contact_phone]
      .map(ph => ph ? ph.slice(-10) : '')
      .filter(ph => ph.length === 10);
    for (const ph of phones) {
      if (!phoneIndex.has(ph)) phoneIndex.set(ph, []);
      phoneIndex.get(ph).push(p);
    }
    // Split multi-email fields (EHR sometimes has "a@x.com; b@x.com")
    const emails = splitEmails(p.contact_email);
    for (const em of emails) {
      if (!emailIndex.has(em)) emailIndex.set(em, []);
      emailIndex.get(em).push(p);
    }
    // Index by last name for fallback matching (store with first name for validation)
    for (const ln of [p.contact_last_name, p.patient_last_name]) {
      if (ln && ln.length > 1) {
        if (!nameIndex.has(ln)) nameIndex.set(ln, []);
        nameIndex.get(ln).push(p);
      }
    }
  }

  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);
  const sevenDaysAgo = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);

  // Track matched EHR patient IDs to avoid double-counting
  const matchedPatientIds = new Set();
  const thisWeekPatientIds = new Set();
  let matchByPhone = 0, matchByEmail = 0, matchByName = 0;

  // Per-source stats
  const bySource = {};
  for (const src of ATTRIBUTION_PRIORITY) {
    bySource[src] = { total: 0, matched: 0, thisWeek: 0 };
  }

  // Class sequencing analysis
  const classStats = {};  // by class label
  const sequencing = { class_first: 0, registration_first: 0, class_only: 0, practice_only: 0 };

  for (const lead of unifiedLeads) {
    const src = lead.attribution;
    if (bySource[src]) bySource[src].total++;

    // Try to match to EHR: phone → email → last name
    let patient = null;
    let matchTier = null;
    if (lead.phone && lead.phone.length === 10 && phoneIndex.has(lead.phone)) {
      patient = phoneIndex.get(lead.phone)[0];
      matchTier = 'phone';
    }
    if (!patient && lead.email) {
      // Try each email from the lead (in case lead has multi-email too)
      const leadEmails = splitEmails(lead.email);
      for (const em of leadEmails) {
        if (emailIndex.has(em)) { patient = emailIndex.get(em)[0]; matchTier = 'email'; break; }
      }
    }
    if (!patient && lead.name) {
      // Last-name fallback: match if last name + first initial exist in EHR
      const leadLast = extractLastName(lead.name);
      const leadFirst = lead.name.trim().split(/\s+/)[0].toLowerCase();
      if (leadLast && leadLast.length > 1 && nameIndex.has(leadLast)) {
        const candidates = nameIndex.get(leadLast);
        // Try first-initial match to avoid false positives
        if (leadFirst) {
          const initialMatch = candidates.find(c => {
            const ehrFirst = c.contact_first_name || c.patient_first_name || '';
            return ehrFirst && ehrFirst[0] === leadFirst[0];
          });
          if (initialMatch) {
            patient = initialMatch;
            matchTier = 'name';
          }
        }
        // If no first-initial match but only 1 patient with this last name, still match
        if (!patient) {
          const uniquePatients = new Set(candidates.map(c => c.patient_id));
          if (uniquePatients.size === 1) {
            patient = candidates[0];
            matchTier = 'name';
          }
        }
      }
    }
    if (matchTier === 'phone') matchByPhone++;
    else if (matchTier === 'email') matchByEmail++;
    else if (matchTier === 'name') matchByName++;

    if (patient && !matchedPatientIds.has(patient.patient_id)) {
      matchedPatientIds.add(patient.patient_id);
      if (bySource[src]) bySource[src].matched++;

      const visitDate = new Date(patient.first_visit_date);
      if (visitDate >= sevenDaysAgo) {
        thisWeekPatientIds.add(patient.patient_id);
        if (bySource[src]) bySource[src].thisWeek++;
      }
    }

    // Class sequencing
    if (lead.classesAttended.length > 0) {
      for (const cls of lead.classesAttended) {
        if (!classStats[cls]) classStats[cls] = { rsvps: 0, becamePatients: 0, classDroveIt: 0 };
        classStats[cls].rsvps++;
      }

      if (patient) {
        // Person attended class AND is a patient
        for (const cls of lead.classesAttended) classStats[cls].becamePatients++;

        // Compare timestamps: class vs registration
        const classTimestamp = lead.firstSeen;
        const regTimestamp = lead.registrationTimestamp ? new Date(lead.registrationTimestamp) : null;

        if (classTimestamp && regTimestamp && !isNaN(regTimestamp.getTime())) {
          if (classTimestamp < regTimestamp) {
            sequencing.class_first++;
            for (const cls of lead.classesAttended) classStats[cls].classDroveIt++;
          } else {
            sequencing.registration_first++;
          }
        } else if (!regTimestamp) {
          // Has patient record but no registration form — class likely drove it
          sequencing.class_first++;
          for (const cls of lead.classesAttended) classStats[cls].classDroveIt++;
        }
      } else {
        sequencing.class_only++;
      }
    } else if (patient) {
      sequencing.practice_only++;
    }
  }

  // Count unattributed new patients (in EHR this week but not in any lead source)
  const allEhrThisWeek = ehrPatients.filter(p => {
    const d = new Date(p.first_visit_date);
    return d >= sevenDaysAgo;
  });
  const unattributed = allEhrThisWeek.filter(p => !thisWeekPatientIds.has(p.patient_id)).length;

  // Class attendees total
  const classAttendeePhones = new Set();
  for (const lead of unifiedLeads) {
    if (lead.classesAttended.length > 0 && lead.phone) classAttendeePhones.add(lead.phone);
  }

  console.log(`  Match breakdown: ${matchByPhone} by phone, ${matchByEmail} by email, ${matchByName} by name`);

  return {
    totalLeads: unifiedLeads.length,
    totalMatchedToEHR: matchedPatientIds.size,
    thisWeekNewPatients: thisWeekPatientIds.size,
    bySource,
    unattributed,
    classAttendees: {
      totalRows: unifiedLeads.filter(l => l.classesAttended.length > 0).length,
      unique: classAttendeePhones.size,
    },
    classStats,
    sequencing,
  };
}

// --- Snapshot persistence ---

function loadSnapshots() {
  try {
    if (existsSync(SNAPSHOT_PATH)) {
      return JSON.parse(readFileSync(SNAPSHOT_PATH, 'utf8'));
    }
  } catch {}
  return [];
}

function saveSnapshot(snapshot) {
  const snapshots = loadSnapshots();
  snapshots.push(snapshot);
  // Keep last 12 weeks
  while (snapshots.length > 12) snapshots.shift();
  try {
    writeFileSync(SNAPSHOT_PATH, JSON.stringify(snapshots, null, 2));
  } catch (e) {
    console.error('Failed to save snapshot:', e.message);
  }
}

// --- Claude prompt ---

async function synthesizeReport(data) {
  const today = new Date();
  const dateStr = today.toLocaleDateString('en-US', {
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
    timeZone: 'America/New_York',
  });

  // Compute the reporting date range (last 7 days ending yesterday)
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  const weekAgo = new Date(yesterday);
  weekAgo.setDate(weekAgo.getDate() - 6);
  const fmt = d => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'America/New_York' });
  const dateRange = `${fmt(weekAgo)} – ${fmt(yesterday)}`;

  const prompt = `You are writing a weekly new patient + marketing funnel report for a 3-office pediatric practice (Warren, Fanwood, Millburn). Today is ${dateStr}. The reporting period is ${dateRange}. This goes to the marketing team and office managers via Slack.

Here is the data:
${JSON.stringify(data, null, 2)}

DEFINITIONS:
- *New patient*: A patient whose very first appointment ever at the practice occurred this week.
- *Newborn*: A new patient who is under 3 months old at their first visit (a new baby, not transferring from another practice).
- *Transfer*: A new patient who is 3+ months old at their first visit (an existing child switching from another pediatrician).
- *Website registration form*: The online form families fill out to start the new patient process. Not all new patients use this form — some call the office directly, get referred by the hospital, etc.

DATA SOURCES:
1. *EHR* (electronic health record — ground truth for who actually visited)
2. *Website Registration Form*: All form submissions. Staff manually tracks each one through stages.
3. *Expecting Parents Tracker*: A curated subset of expecting families tracked separately.
4. *Facebook Lead Ads*: Leads captured directly from Facebook ad forms.
5. *Class RSVP Sheets*: Signups for new parent classes (BSCWI, Scott, De Los Rios). Each class asks "how did you hear about us?" — this is used for attribution.

DATA DICTIONARY:
- "newPatientsThisWeek": First-ever visits by office, split newborns vs transfers
- "weeklyTrends": Same data for the last 8 weeks
- "leadSources": Unified lead attribution — all sources merged by phone number. Contains:
  - "bySource": Breakdown by attribution channel (facebook_ad, instagram, google_search, email_marketing, employee_referral, word_of_mouth, website, class_only, unknown). Each has total leads, matched to EHR (became patients in last 90 days), and thisWeek (became patients this week).
  - "classStats": Per-class breakdown: rsvps, becamePatients, classDroveIt (signed up for class BEFORE registering with practice)
  - "sequencing": class_first (class drove them to practice), registration_first (already joining, class was bonus), class_only (attended class, not yet a patient), practice_only (patient, no class)
  - "unattributed": New patients this week NOT matched to any lead source (walked in, hospital referral, etc.)
- "registrationFunnel": Cumulative funnel — each stage shows how many registrants progressed at least that far
- "expectingFunnel": Same cumulative structure for expecting parents
- "submissionsThisWeek" / "submissionTrend": Form submission volume

Return a JSON array with exactly 1 object: {"header":"...","sections":[...],"fallback":"..."}

Sections use Slack mrkdwn. Special prefixes:
- "---" = divider block
- "context: ..." = small gray text

REPORT STRUCTURE:

1. *New Patients — ${dateRange}*
- Include the date range in the section header
- Start with a "context:" line: "Source: EHR (actual first appointments). A new patient = someone whose very first visit ever at the practice happened this week."
- Code block table: Office | Total | Newborns | Transfers
- One-line total and comparison to 8-week average
- "---"

2. *Lead Sources*
- Start with a "context:" line explaining the columns: "How we track where patients came from. *Leads* = people who contacted us through this channel (all time). *Patients* = leads who became actual patients (last 90 days). *This Week* = leads who had their first appointment this week. Sources: <https://docs.google.com/spreadsheets/d/135Pa6HW5ogh8gduJwxTUtEnZZVHo25f4o3L0m2fCZsA|Registration Sheet> | <https://docs.google.com/spreadsheets/d/12alQPnQydxVbl2DEjxGdtKJPtnC5xJ5yoJI3rnE1qBM|Class RSVPs> | <https://docs.google.com/spreadsheets/d/1ZZ2xxqwb2f0-ngriygb4YcXEXPNvGilVQwUkDTwBaNo|BSCWI> | Facebook Lead Ads"
- Code block table:
\`\`\`
Source             | Leads | Patients | This Week
Facebook Ads       |    53 |        5 |         1
Instagram          |     4 |        0 |         0
Google Search      |     3 |        1 |         1
Email/Listserv     |     5 |        2 |         0
Employee Referral  |     2 |        1 |         0
Word of Mouth      |     8 |        3 |         1
Website Form       |   184 |       70 |        18
Class Only         |    23 |        4 |         2
No lead source     |     — |        — |         2
\`\`\`
- Use data from leadSources.bySource. "Leads" = total, "Patients" = matched (became patients in 90 days), "This Week" = thisWeek.
- "No lead source" row uses leadSources.unattributed for the This Week column. Leads and Patients columns show "—".
- One summary line: "X of Y new patients this week came from a known lead source"
- Only include source rows that have at least 1 lead (total > 0). Skip empty rows.
- "---"

3. *Classes*
- Start with a "context:" line: "New parent class signups matched to EHR by phone. 'Class Drove It' = person signed up for the class before registering with the practice. Sources: <https://docs.google.com/spreadsheets/d/12alQPnQydxVbl2DEjxGdtKJPtnC5xJ5yoJI3rnE1qBM|Class RSVPs> | <https://docs.google.com/spreadsheets/d/1ZZ2xxqwb2f0-ngriygb4YcXEXPNvGilVQwUkDTwBaNo|BSCWI>"
- Code block table:
\`\`\`
Class                    | RSVPs | Became Patients | Class Drove It
Scott 2/4 Class          |    38 |               8 |              5
BSCWI Jan                |    27 |               6 |              4
\`\`\`
- Use data from leadSources.classStats. Only include classes with rsvps > 0.
- Summary line: "X of Y class attendees became patients. Z signed up for the class before registering."
- Use leadSources.sequencing for the summary totals.
- 6 lines max
- "---"

4. *Registration Funnel*
- Start with a "context:" line: "All-time funnel from <https://docs.google.com/spreadsheets/d/135Pa6HW5ogh8gduJwxTUtEnZZVHo25f4o3L0m2fCZsA|website registration form>. Each stage shows how many people made it at least that far."
- Render the cumulativeFunnel as a narrowing visual:
\`\`\`
Submitted              419  100%
 → Contact Attempted   301   72%  (72% of submitted)
   → Connected         289   69%  (96% of attempted)
     → Docs Received   256   61%  (89% of connected)
       → Ready to Sch  155   37%  (61% of docs)
         → Scheduled   147   35%  (95% of ready)
\`\`\`
- One line calling out the biggest drop-off (biggestDrop)
- One line: exits total (declined + unreachable + on hold)
- One line: new form submissions this week
- "---"

5. *Stuck Registrations*
- Compact: how many not contacted >14 days, pending docs >30 days, left message >21 days
- By-office table: Office | Total | Contact Rate | Scheduled Rate
- Call out worst-performing office
- 5 lines max
- "---"

6. *Expecting Parents*
- Start with a "context:" line: "Expecting families tracked in the <https://docs.google.com/spreadsheets/d/135Pa6HW5ogh8gduJwxTUtEnZZVHo25f4o3L0m2fCZsA|Expecting Parents tab>. Staff manually updates stages."
- Compact cumulative funnel (same format, smaller numbers)
- One line: stuck count + upcoming due dates in 2 weeks
- One line: which offices have the most not-contacted expecting parents
- 5 lines max
- "---"

7. *8-Week Trends*
- Start with a "context:" line: "All columns from EHR except Form Subs (from <https://docs.google.com/spreadsheets/d/135Pa6HW5ogh8gduJwxTUtEnZZVHo25f4o3L0m2fCZsA|registration sheet>). New Pts = first-ever appointment that week. Newborn = under 3 months old at first visit. Transfer = 3+ months old (switching from another pediatrician). Form Subs = new website registration form submissions that week."
- Code block table: Week | New Pts | Newborns | Transfers | Form Subs
- Aggregate across offices. Keep it tight.
- "---"

8. *Action Items*
- 3-4 numbered items. Specific numbers. What to do about it.
- Lead with the most urgent operational issue.

RULES:
- *bold* for key numbers. \`code\` for metrics inline.
- Tables in code blocks, clean column alignment.
- BE CONCISE. Each section should be 3-6 lines. No padding, no filler, no restating what the table already shows.
- Do NOT add "observations" or "analysis" paragraphs after tables — the numbers speak for themselves. Only add a line if it highlights something the reader wouldn't notice from the table alone.
- NO PHI. Aggregate counts only.
- Use plain language: "X came from Facebook ads", "Y found us through Google". No jargon.
- The funnel conversion rates are the KEY insight — make them prominent.
- NO EMOJIS anywhere. No emoji in headers, body text, or action items. Zero emojis.
- Sections must be plain strings, NOT objects. Do not return {"type":"mrkdwn","text":"..."} — just return the string itself.
- "---" means a divider — return it as the literal string "---", not as an object.
- Return valid JSON: [{"header":"...","sections":["string1","---","string2",...],"fallback":"..."}]`;

  const { messages } = await callClaude(prompt, { maxTokens: 4096 });
  return messages;
}

// --- Main ---

async function main() {
  const start = Date.now();
  console.log(`Weekly marketing report — ${new Date().toISOString()}`);
  if (DRY_RUN) console.log('(dry run mode)');

  // 1. Read Google Sheets (registration + expecting)
  console.log('Reading Google Sheets...');
  const sheets = await getSheets();
  const [expectingRaw, formRaw] = await Promise.all([
    readSheetTab(sheets, 'Expecting Parents', 'A1:H500'),
    readSheetTab(sheets, 'Form Responses 1', 'A1:Z500'),
  ]);

  const expectingLeads = parseExpectingParents(expectingRaw);
  const formResponses = parseFormResponses(formRaw);
  console.log(`  Expecting Parents: ${expectingLeads.length} leads`);
  console.log(`  Form Responses: ${formResponses.length} (${formResponses.filter(f => f.isExpecting).length} expecting, ${formResponses.filter(f => f.isRegistering).length} registering)`);

  // 2. Read class RSVP sheets
  console.log('Reading class RSVP sheets...');
  const classRSVPs = await fetchAllClassRSVPs(sheets);
  console.log(`  Class RSVPs: ${classRSVPs.all.length} total rows, ${classRSVPs.unique.length} unique people`);

  // 3. Run EHR queries
  console.log('Running EHR queries...');
  const queryKeys = Object.keys(QUERIES);
  const queryResults = await Promise.all(queryKeys.map(k => pool.query(QUERIES[k])));
  const ehrData = {};
  queryKeys.forEach((k, i) => {
    ehrData[k] = queryResults[i].rows;
  });
  console.log(`  Query results: ${queryKeys.map((k, i) => `${k}=${queryResults[i].rowCount}`).join(', ')}`);

  // 4. Fetch Facebook leads
  console.log('Fetching Facebook leads...');
  const fbLeads = await fetchFBLeads();
  console.log(`  FB leads: ${fbLeads.length}`);

  // 5. Build unified lead list
  console.log('Building unified lead list...');
  const unifiedLeads = buildUnifiedLeadList(fbLeads, classRSVPs, formResponses, expectingLeads);
  console.log(`  Unified leads: ${unifiedLeads.length} unique people`);
  // Log attribution breakdown
  const attrCounts = {};
  for (const l of unifiedLeads) attrCounts[l.attribution] = (attrCounts[l.attribution] || 0) + 1;
  console.log(`  Attribution: ${Object.entries(attrCounts).map(([k, v]) => `${k}=${v}`).join(', ')}`);

  // 6. Match unified leads to EHR
  console.log('Matching leads to EHR...');
  const leadMatchResults = matchLeadsToEHR(unifiedLeads, ehrData.newPatientsForMatching);
  console.log(`  Matched: ${leadMatchResults.totalMatchedToEHR} of ${leadMatchResults.totalLeads} leads`);
  console.log(`  This week: ${leadMatchResults.thisWeekNewPatients} from known sources, ${leadMatchResults.unattributed} unattributed`);

  // 7. Analyze funnels (kept as-is)
  const expectingFunnel = analyzeExpectingFunnel(expectingLeads);
  const registrationFunnel = analyzeRegistrationFunnel(formResponses);
  const submissionsThisWeek = countSubmissionsThisWeek(formResponses);
  const submissionTrend = weeklySubmissionTrend(formResponses);

  // 8. Load prior snapshots for trend
  const priorSnapshots = loadSnapshots();

  // 9. Build report data
  const reportData = {
    newPatientsThisWeek: ehrData.newPatientsThisWeek,
    weeklyTrends: ehrData.weeklyTrends,
    leadSources: leadMatchResults,
    registrationFunnel,
    expectingFunnel,
    submissionsThisWeek,
    submissionTrend,
    priorSnapshots: priorSnapshots.slice(-4),
  };

  if (DRY_RUN) {
    console.log('\n=== RAW DATA FOR CLAUDE ===');
    console.log(JSON.stringify(reportData, null, 2));
  }

  // 10. Claude synthesis
  console.log('Sending to Claude...');
  const messages = await synthesizeReport(reportData);

  // 11. Post to Slack
  console.log('Posting to Slack...');
  for (const msg of messages) {
    const blocks = buildBlocks(msg);
    await postBlocks(blocks, msg.fallback || 'Weekly Marketing Report', CHANNEL, { dryRun: DRY_RUN });
  }

  // 12. Save snapshot
  const snapshot = {
    date: new Date().toISOString().slice(0, 10),
    leadSources: {
      bySource: leadMatchResults.bySource,
      totalLeads: leadMatchResults.totalLeads,
      totalMatchedToEHR: leadMatchResults.totalMatchedToEHR,
      thisWeekNewPatients: leadMatchResults.thisWeekNewPatients,
      unattributed: leadMatchResults.unattributed,
    },
    registrationFunnel: {
      cumulativeFunnel: registrationFunnel.cumulativeFunnel,
      currentCounts: registrationFunnel.currentCounts,
      stuck: registrationFunnel.stuck,
      total: registrationFunnel.total,
    },
    expectingFunnel: {
      cumulativeFunnel: expectingFunnel.cumulativeFunnel,
      stageCounts: expectingFunnel.stageCounts,
      stuckCount: expectingFunnel.stuckCount,
      totalLeads: expectingFunnel.totalLeads,
    },
    submissionsThisWeek,
    newPatients: ehrData.newPatientsThisWeek,
  };
  saveSnapshot(snapshot);

  const totalMs = Date.now() - start;
  console.log(`Done in ${totalMs}ms.`);
  await pool.end();
}

main().catch(async (e) => {
  console.error('Fatal:', e);
  if (!DRY_RUN) {
    try {
      const blocks = [{ type: 'section', text: { type: 'mrkdwn', text: `Weekly marketing report failed: ${e.message}` } }];
      await postBlocks(blocks, 'Weekly marketing report failed', CHANNEL);
    } catch {}
  }
  await pool.end();
  process.exit(1);
});
