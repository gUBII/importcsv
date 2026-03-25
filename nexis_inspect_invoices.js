/**
 * Inspect Nexis365 paid invoices page — fill filters and search.
 */

const { chromium } = require("playwright");
const path = require("path");
const fs = require("fs");

const LOGIN_URL = "https://nexis365.com/cohs/login.php";
const PAID_URL = "https://nexis365.com/saas/index.php?url=paid_invoices.php&id=5163";
const ERRORS_DIR = path.resolve(__dirname, "errors");

async function main() {
  const username = process.env.NEXIS_USERNAME;
  const password = process.env.NEXIS_PASSWORD;

  if (!fs.existsSync(ERRORS_DIR)) fs.mkdirSync(ERRORS_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: false, slowMo: 300 });
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);

  // Login
  console.log("[1] Logging in...");
  await page.goto(LOGIN_URL, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.locator("input[name='uid']").fill(username);
  await page.locator("input[name='upass']").fill(password);
  await page.locator("button[type='submit']").click();
  await page.waitForURL(/index\.php/, { timeout: 30_000, waitUntil: "domcontentloaded" });
  console.log("[1] Logged in.");

  // Navigate to paid invoices
  console.log("[2] Navigating to paid invoices...");
  await page.goto(PAID_URL, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(2_000);

  // Remove overlays
  await page.evaluate(() => {
    document.querySelectorAll("div[id*='load'],div[class*='load'],div[class*='overlay'],div[id*='overlay']")
      .forEach(el => el.remove());
    document.body.style.overflow = "auto";
  }).catch(() => {});

  // Dump the client dropdown options
  const clientOptions = await page.evaluate(() => {
    const sel = document.querySelector("select[name='cname']");
    if (!sel) return [];
    return [...sel.options].map(o => ({ value: o.value, text: o.textContent.trim() }));
  });
  console.log("\n=== CLIENT DROPDOWN OPTIONS ===");
  clientOptions.forEach(o => console.log(`  value="${o.value}" → "${o.text}"`));

  // Dump the ledger dropdown options
  const ledgerOptions = await page.evaluate(() => {
    const sel = document.querySelector("select[name='lid']");
    if (!sel) return [];
    return [...sel.options].map(o => ({ value: o.value, text: o.textContent.trim() }));
  });
  console.log("\n=== LEDGER DROPDOWN OPTIONS ===");
  ledgerOptions.forEach(o => console.log(`  value="${o.value}" → "${o.text}"`));

  // Set dates to a wider range
  console.log("\n[3] Setting date range and selecting all clients...");
  await page.locator("input[name='lid_date_from']").fill("2025-01-01");
  await page.locator("input[name='lid_date_to']").fill("2026-03-25");

  // Select "All Clients" from the client dropdown
  const clientSelect = page.locator("select[name='cname']");
  console.log('  Selecting "All Clients" (value="ALL")');
  await clientSelect.selectOption("ALL");

  await page.screenshot({ path: path.join(ERRORS_DIR, "07_before_search.png"), fullPage: true });

  // Click Search
  console.log("[3] Clicking Search...");
  await page.locator("input[value='Search']").click();
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(3_000);

  // Remove overlays again
  await page.evaluate(() => {
    document.querySelectorAll("div[id*='load'],div[class*='load'],div[class*='overlay'],div[id*='overlay']")
      .forEach(el => el.remove());
    document.body.style.overflow = "auto";
  }).catch(() => {});

  await page.screenshot({ path: path.join(ERRORS_DIR, "08_after_search.png"), fullPage: true });
  console.log("[3] Screenshot: errors/08_after_search.png");

  // Dump all tables after search
  const tableInfo = await page.evaluate(() => {
    const results = [];
    document.querySelectorAll("table").forEach((t, i) => {
      const headers = [...t.querySelectorAll("th")].map(th => th.textContent.trim());
      const bodyRows = t.querySelectorAll("tbody tr");
      const rowCount = bodyRows.length;
      const allRows = bodyRows.length > 0 ? bodyRows : t.querySelectorAll("tr");
      const sampleRows = [];
      for (let r = 0; r < Math.min(3, allRows.length); r++) {
        const cells = [...allRows[r].querySelectorAll("td, th")].map(c => c.textContent.trim().slice(0, 80));
        const links = [...allRows[r].querySelectorAll("a")].map(a => ({
          text: a.textContent.trim().slice(0, 50),
          href: a.href,
          onclick: a.getAttribute("onclick"),
        }));
        sampleRows.push({ cells, links });
      }
      // Only include tables that seem relevant (have some content)
      if (headers.length > 0 || rowCount > 0) {
        results.push({ index: i, id: t.id, class: t.className, headers, rowCount, sampleRows });
      }
    });
    return results;
  });

  console.log("\n=== TABLES WITH CONTENT (after search) ===");
  tableInfo.forEach(t => console.log(JSON.stringify(t, null, 2)));

  // Also check #accordion which was mentioned in the original prompt
  const accordionInfo = await page.evaluate(() => {
    const acc = document.querySelector("#accordion");
    if (!acc) return null;
    return {
      tagName: acc.tagName,
      childCount: acc.children.length,
      innerHTML: acc.innerHTML.slice(0, 3000),
    };
  });
  if (accordionInfo) {
    console.log("\n=== #ACCORDION ELEMENT ===");
    console.log(JSON.stringify(accordionInfo, null, 2));
  } else {
    console.log("\n[info] No #accordion element found on this page.");
  }

  // Check for pagination
  const pagination = await page.evaluate(() => {
    const pagElems = document.querySelectorAll(".pagination, nav[aria-label*='page'], [class*='pagi']");
    return [...pagElems].map(p => ({ tag: p.tagName, class: p.className, html: p.innerHTML.slice(0, 500) }));
  });
  console.log("\n=== PAGINATION ===");
  pagination.forEach(p => console.log(JSON.stringify(p)));

  // Save full HTML
  const html = await page.content();
  fs.writeFileSync(path.join(ERRORS_DIR, "paid_invoices_searched.html"), html);
  console.log("\n[4] Full HTML saved: errors/paid_invoices_searched.html");

  // If we find invoice rows, try clicking the first View
  const viewLinks = await page.locator("a:has-text('View')").count();
  console.log(`\n[5] Found ${viewLinks} "View" links on page.`);

  if (viewLinks > 0) {
    console.log("[5] Clicking first View link...");
    await page.locator("a:has-text('View')").first().click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.waitForTimeout(3_000);

    await page.evaluate(() => {
      document.querySelectorAll("div[id*='load'],div[class*='load'],div[class*='overlay'],div[id*='overlay']")
        .forEach(el => el.remove());
      document.body.style.overflow = "auto";
    }).catch(() => {});

    await page.screenshot({ path: path.join(ERRORS_DIR, "09_invoice_viewer.png"), fullPage: true });
    console.log("[5] Screenshot: errors/09_invoice_viewer.png");

    // Dump buttons/inputs in the invoice viewer
    const viewerBtns = await page.evaluate(() => {
      return [...document.querySelectorAll("button, input[type='submit'], input[type='button'], a.btn")]
        .map(el => ({
          tag: el.tagName, type: el.type, text: el.textContent.trim().slice(0, 100),
          value: el.value || "", id: el.id, class: el.className, onclick: el.getAttribute("onclick"),
          href: el.href || "",
        }));
    });
    console.log("\n=== INVOICE VIEWER BUTTONS/INPUTS ===");
    viewerBtns.filter(b => b.text || b.value).forEach(b => console.log(JSON.stringify(b)));

    const viewerHTML = await page.content();
    fs.writeFileSync(path.join(ERRORS_DIR, "invoice_viewer.html"), viewerHTML);
    console.log("[5] Full HTML saved: errors/invoice_viewer.html");
  }

  console.log("\n[info] Browser stays open for 60 seconds...");
  await page.waitForTimeout(60_000);
  await browser.close();
}

main().catch(err => { console.error("Error:", err); process.exit(1); });
