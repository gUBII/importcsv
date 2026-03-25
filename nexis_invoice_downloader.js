/**
 * Nexis365 Invoice PDF Downloader
 *
 * Downloads all printable invoice PDFs from the paid invoices page
 * for a given date range.
 *
 * Usage:
 *   NEXIS_USERNAME=you@example.com NEXIS_PASSWORD=secret node nexis_invoice_downloader.js
 *
 * Environment variables:
 *   NEXIS_USERNAME  - Nexis365 login email (required)
 *   NEXIS_PASSWORD  - Nexis365 login password (required)
 *   HEADFUL=1       - Run with visible browser (default: headless)
 *   DOWNLOAD_DIR    - Custom download directory (default: ./downloads)
 */

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const readline = require("readline");

// ── Config ──────────────────────────────────────────────────────────────────

const LOGIN_BASE = "https://nexis365.com/cohs";
const APP_BASE = "https://nexis365.com/saas";
const LOGIN_URL = `${LOGIN_BASE}/login.php`;
const UNPAID_INVOICES_URL = `${APP_BASE}/index.php?url=unpaid_invoices.php&id=5162`;
const PAID_INVOICES_URL = `${APP_BASE}/index.php?url=paid_invoices.php&id=5163`;
const DOWNLOAD_DIR = process.env.DOWNLOAD_DIR || path.resolve(__dirname, "downloads");
const ERRORS_DIR = path.resolve(__dirname, "errors");
const HEADLESS = process.env.HEADFUL !== "1";
const TIMEOUT = 30_000;

// ── Helpers ─────────────────────────────────────────────────────────────────

function ensureDir(dir) {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function sanitizeFilename(name) {
  return name
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/\s+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 200);
}

function uniquePath(dir, base, ext) {
  let candidate = path.join(dir, `${base}${ext}`);
  let counter = 1;
  while (fs.existsSync(candidate)) {
    candidate = path.join(dir, `${base}_${counter}${ext}`);
    counter++;
  }
  return candidate;
}

function askQuestion(rl, question) {
  return new Promise((resolve) => rl.question(question, resolve));
}

async function askDateRange() {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  try {
    const startDate = await askQuestion(rl, "Start date (DD/MMM/YYYY, e.g. 01/Jan/2026): ");
    const endDate = await askQuestion(rl, "End date   (DD/MMM/YYYY, e.g. 31/Mar/2026): ");
    if (!startDate.trim() || !endDate.trim()) {
      throw new Error("Both start and end dates are required.");
    }
    return { startDate: startDate.trim(), endDate: endDate.trim() };
  } finally {
    rl.close();
  }
}

async function screenshotOnError(page, label) {
  try {
    ensureDir(ERRORS_DIR);
    const ts = new Date().toISOString().replace(/[:.]/g, "-");
    const filename = `error_${sanitizeFilename(label)}_${ts}.png`;
    await page.screenshot({ path: path.join(ERRORS_DIR, filename), fullPage: true });
    console.log(`  [screenshot] ${filename}`);
  } catch {
    // best-effort
  }
}

async function waitForOverlaysClear(page) {
  try {
    await page.waitForFunction(
      () => {
        const overlays = document.querySelectorAll(
          "div[id*='load'],div[class*='load'],div[class*='overlay'],div[id*='overlay']"
        );
        return [...overlays].every((el) => {
          const style = window.getComputedStyle(el);
          return style.display === "none" || style.visibility === "hidden" || style.opacity === "0";
        });
      },
      { timeout: 10_000 }
    ).catch(() => {});
  } catch {
    // continue anyway
  }
}

async function removeOverlays(page) {
  await page.evaluate(() => {
    document
      .querySelectorAll("div[id*='load'],div[class*='load'],div[class*='overlay'],div[id*='overlay']")
      .forEach((el) => el.remove());
    document.body.style.overflow = "auto";
  }).catch(() => {});
}

// ── Core workflow ───────────────────────────────────────────────────────────

async function login(page, username, password) {
  console.log("[login] Navigating to login page...");
  await page.goto(LOGIN_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await page.waitForLoadState("networkidle").catch(() => {});

  // Nexis365 login form: uid (text) + upass (password), POSTs to security.php
  await page.locator("input[name='uid']").fill(username);
  await page.locator("input[name='upass']").fill(password);
  await page.locator("button[type='submit']").click();

  // After login, redirects through security.php → cookie.php → /saas/index.php
  await page.waitForURL(/index\.php/, { timeout: TIMEOUT, waitUntil: "domcontentloaded" });
  console.log("[login] Success.");
}

async function applyDateFilter(page, startDate, endDate) {
  await waitForOverlaysClear(page);
  await removeOverlays(page);

  // Fill From Date
  const fromField =
    page.locator("input[name='from_date']").or(
      page.locator("input[name='fromdate']")
    ).or(
      page.getByLabel(/from date/i)
    ).or(
      page.locator("input[placeholder*='From']")
    ).first();

  // Fill To Date
  const toField =
    page.locator("input[name='to_date']").or(
      page.locator("input[name='todate']")
    ).or(
      page.getByLabel(/to date/i)
    ).or(
      page.locator("input[placeholder*='To']")
    ).first();

  try {
    await fromField.waitFor({ state: "visible", timeout: 10_000 });
  } catch {
    console.log("  [warn] From Date field not immediately visible, trying to proceed...");
  }

  await fromField.fill("");
  await fromField.fill(startDate);
  await toField.fill("");
  await toField.fill(endDate);

  // Click Search
  const searchBtn =
    page.getByRole("button", { name: /search/i }).or(
      page.locator("button:has-text('Search')")
    ).or(
      page.locator("input[value='Search']")
    ).or(
      page.locator("button[type='submit']")
    ).first();

  await searchBtn.click();
  await page.waitForLoadState("networkidle").catch(() => {});
  await waitForOverlaysClear(page);
  await removeOverlays(page);
}

async function getInvoiceRows(page) {
  // Re-query every time to avoid stale handles
  await removeOverlays(page);

  // Find the invoices table — look for a table inside #accordion or the main content
  const table =
    page.locator("#accordion table").or(
      page.locator("table.table")
    ).or(
      page.locator("table")
    ).first();

  await table.waitFor({ state: "visible", timeout: 10_000 }).catch(() => {});

  // Get all body rows
  const rows = table.locator("tbody tr");
  const count = await rows.count();
  return { rows, count };
}

function extractRowData(cells) {
  // Expected columns: Invoice ID | Post Date | Template ID | Invoice No. | Client Name | Total Received | View
  // Indices may vary — we extract what we can
  return {
    invoiceId: cells[0] || "unknown",
    postDate: cells[1] || "",
    invoiceNo: cells[3] || cells[0] || "unknown",
    clientName: cells[4] || "unknown",
  };
}

async function processInvoiceRow(page, rowIndex, stats) {
  const { rows, count } = await getInvoiceRows(page);
  if (rowIndex >= count) return false;

  const row = rows.nth(rowIndex);

  // Extract cell text for filename
  const cells = await row.locator("td").allTextContents();
  const data = extractRowData(cells.map((c) => c.trim()));
  const label = `${data.invoiceNo}_${data.clientName}`;
  const safeLabel = sanitizeFilename(label);

  // Check if already downloaded
  const existing = fs
    .readdirSync(DOWNLOAD_DIR)
    .find((f) => f.startsWith(safeLabel) && f.endsWith(".pdf"));
  if (existing) {
    console.log(`  [skip] ${safeLabel} already exists`);
    stats.skipped++;
    return true;
  }

  console.log(`  [${rowIndex + 1}/${count}] Invoice ${data.invoiceNo} — ${data.clientName}`);

  // Click View link in this row
  const viewLink =
    row.getByRole("link", { name: /view/i }).or(
      row.locator("a:has-text('View')")
    ).or(
      row.locator("a").last()
    ).first();

  try {
    await viewLink.click({ timeout: 5_000 });
  } catch (err) {
    // Fallback: JS click
    const el = await viewLink.elementHandle();
    if (el) {
      await page.evaluate((e) => e.click(), el);
    } else {
      throw err;
    }
  }

  // Wait for invoice viewer to load (modal or new page section)
  await page.waitForLoadState("networkidle").catch(() => {});
  await waitForOverlaysClear(page);
  await removeOverlays(page);

  // Wait a moment for content to render
  await page.waitForTimeout(1_000);

  // Locate Print Invoice button
  const printBtn =
    page.locator("input[value='Print Invoice']").or(
      page.getByRole("button", { name: /print invoice/i })
    ).or(
      page.locator("button:has-text('Print Invoice')")
    ).or(
      page.locator("a:has-text('Print Invoice')")
    ).first();

  try {
    await printBtn.waitFor({ state: "visible", timeout: 10_000 });
  } catch {
    console.log("    [warn] Print Invoice button not found, trying alternate selectors...");
    await screenshotOnError(page, `no_print_btn_${safeLabel}`);
    stats.failed++;
    // Try to go back
    await navigateBack(page);
    return true;
  }

  // Set up download promise BEFORE clicking print
  const downloadPromise = page.waitForEvent("download", { timeout: TIMEOUT });

  // Handle potential popup/new tab
  const popupPromise = page.context().waitForEvent("page", { timeout: 5_000 }).catch(() => null);

  await printBtn.click();

  // Check if a popup opened (print might open in new tab)
  const popup = await popupPromise;

  let download;
  if (popup) {
    // PDF opened in new tab — try to get download from there
    console.log("    [info] Print opened in new tab, capturing download...");
    try {
      download = await popup.waitForEvent("download", { timeout: TIMEOUT });
    } catch {
      // If no download event, the tab might show the PDF directly — use print-to-pdf
      try {
        const pdfPath = uniquePath(DOWNLOAD_DIR, safeLabel, ".pdf");
        const pdfBuffer = await popup.pdf({ format: "A4" });
        fs.writeFileSync(pdfPath, pdfBuffer);
        console.log(`    [saved] ${path.basename(pdfPath)} (via page.pdf)`);
        stats.downloaded++;
        await popup.close();
        await navigateBack(page);
        return true;
      } catch (pdfErr) {
        console.log(`    [warn] Could not capture PDF from popup: ${pdfErr.message}`);
        await popup.close().catch(() => {});
        await screenshotOnError(page, `popup_fail_${safeLabel}`);
        stats.failed++;
        await navigateBack(page);
        return true;
      }
    }
    await popup.close().catch(() => {});
  } else {
    try {
      download = await downloadPromise;
    } catch {
      console.log("    [warn] No download event received, trying page.pdf fallback...");
      await screenshotOnError(page, `no_download_${safeLabel}`);
      stats.failed++;
      await navigateBack(page);
      return true;
    }
  }

  // Save the download
  if (download) {
    const destPath = uniquePath(DOWNLOAD_DIR, safeLabel, ".pdf");
    await download.saveAs(destPath);
    console.log(`    [saved] ${path.basename(destPath)}`);
    stats.downloaded++;
  }

  await navigateBack(page);
  return true;
}

async function navigateBack(page) {
  // Try closing any open modal first
  const closeBtn = page.locator("button.btn-close, button.close, [data-bs-dismiss='modal']").first();
  try {
    if (await closeBtn.isVisible({ timeout: 1_000 })) {
      await closeBtn.click();
      await page.waitForTimeout(500);
      return;
    }
  } catch {
    // no modal to close
  }

  // Go back to the invoices list
  await page.goBack({ waitUntil: "domcontentloaded" }).catch(async () => {
    // If goBack fails, navigate directly
    await page.goto(PAID_INVOICES_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  });
  await page.waitForLoadState("networkidle").catch(() => {});
  await waitForOverlaysClear(page);
  await removeOverlays(page);
}

async function goToNextPageIfAny(page) {
  // Look for pagination next button
  const nextBtn =
    page.locator("a:has-text('Next')").or(
      page.locator("li.next a")
    ).or(
      page.locator(".pagination a:has-text('›')")
    ).or(
      page.locator(".pagination a:has-text('»')")
    ).or(
      page.locator("a[rel='next']")
    ).first();

  try {
    const isVisible = await nextBtn.isVisible({ timeout: 2_000 });
    if (!isVisible) return false;

    // Check if it's disabled
    const parentLi = nextBtn.locator("xpath=..");
    const parentClass = await parentLi.getAttribute("class").catch(() => "");
    if (parentClass && parentClass.includes("disabled")) return false;

    await nextBtn.click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await waitForOverlaysClear(page);
    await removeOverlays(page);
    return true;
  } catch {
    return false;
  }
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const username = process.env.NEXIS_USERNAME;
  const password = process.env.NEXIS_PASSWORD;
  if (!username || !password) {
    console.error("Error: NEXIS_USERNAME and NEXIS_PASSWORD environment variables are required.");
    process.exit(1);
  }

  const { startDate, endDate } = await askDateRange();
  console.log(`\n[config] Date range: ${startDate} → ${endDate}`);
  console.log(`[config] Download dir: ${DOWNLOAD_DIR}`);
  console.log(`[config] Headless: ${HEADLESS}\n`);

  ensureDir(DOWNLOAD_DIR);
  ensureDir(ERRORS_DIR);

  const browser = await chromium.launch({ headless: HEADLESS });
  const context = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1400, height: 900 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);

  const stats = { attempted: 0, downloaded: 0, skipped: 0, failed: 0 };

  try {
    // Step 1: Login
    await login(page, username, password);

    // Step 2: Visit unpaid invoices first (as per workflow)
    console.log("\n[unpaid] Navigating to unpaid invoices...");
    await page.goto(UNPAID_INVOICES_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
    await page.waitForLoadState("networkidle").catch(() => {});
    await waitForOverlaysClear(page);
    await removeOverlays(page);
    await applyDateFilter(page, startDate, endDate);
    console.log("[unpaid] Date filter applied. Moving to paid invoices...\n");

    // Step 3: Navigate to paid invoices
    console.log("[paid] Navigating to paid invoices...");
    await page.goto(PAID_INVOICES_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
    await page.waitForLoadState("networkidle").catch(() => {});
    await waitForOverlaysClear(page);
    await removeOverlays(page);
    await applyDateFilter(page, startDate, endDate);
    console.log("[paid] Date filter applied. Starting downloads...\n");

    // Step 4: Process all invoice rows, page by page
    let pageNum = 1;
    let hasMore = true;

    while (hasMore) {
      console.log(`[page ${pageNum}] Processing...`);
      const { count } = await getInvoiceRows(page);

      if (count === 0) {
        console.log(`[page ${pageNum}] No invoice rows found.`);
        break;
      }

      console.log(`[page ${pageNum}] Found ${count} invoice rows.`);

      for (let i = 0; i < count; i++) {
        stats.attempted++;
        try {
          const continued = await processInvoiceRow(page, i, stats);
          if (!continued) break;

          // After navigating back, we may need to re-apply the date filter
          // if the page state was lost
          const { count: newCount } = await getInvoiceRows(page).catch(() => ({
            count: 0,
          }));
          if (newCount === 0) {
            console.log("  [info] Re-applying date filter after navigation...");
            await applyDateFilter(page, startDate, endDate);
          }
        } catch (err) {
          console.log(`  [error] Row ${i + 1}: ${err.message}`);
          await screenshotOnError(page, `row_${i + 1}_page_${pageNum}`);
          stats.failed++;

          // Try to recover by going back to paid invoices
          try {
            await page.goto(PAID_INVOICES_URL, {
              waitUntil: "domcontentloaded",
              timeout: TIMEOUT,
            });
            await page.waitForLoadState("networkidle").catch(() => {});
            await applyDateFilter(page, startDate, endDate);
          } catch {
            console.log("  [error] Could not recover, stopping this page.");
            break;
          }
        }
      }

      // Try next page
      hasMore = await goToNextPageIfAny(page);
      if (hasMore) {
        pageNum++;
      }
    }
  } catch (err) {
    console.error(`\n[fatal] ${err.message}`);
    await screenshotOnError(page, "fatal");
  } finally {
    await browser.close();
  }

  // Summary
  console.log("\n" + "=".repeat(50));
  console.log("  DOWNLOAD SUMMARY");
  console.log("=".repeat(50));
  console.log(`  Attempted:  ${stats.attempted}`);
  console.log(`  Downloaded: ${stats.downloaded}`);
  console.log(`  Skipped:    ${stats.skipped}`);
  console.log(`  Failed:     ${stats.failed}`);
  console.log("=".repeat(50));
}

main().catch((err) => {
  console.error("Unhandled error:", err);
  process.exit(1);
});
