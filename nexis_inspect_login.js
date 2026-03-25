/**
 * Quick headful inspection of Nexis365 login page.
 * Dumps form HTML, takes screenshots, and attempts login.
 *
 * Usage:
 *   NEXIS_USERNAME="AD275A23" NEXIS_PASSWORD="1234567" node nexis_inspect_login.js
 */

const { chromium } = require("playwright");
const path = require("path");
const fs = require("fs");

const BASE_URL = "https://nexis365.com/cohs";
const LOGIN_URL = `${BASE_URL}/login.php`;
const ERRORS_DIR = path.resolve(__dirname, "errors");

async function main() {
  const username = process.env.NEXIS_USERNAME;
  const password = process.env.NEXIS_PASSWORD;

  if (!fs.existsSync(ERRORS_DIR)) fs.mkdirSync(ERRORS_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: false, slowMo: 500 });
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);

  console.log("[1] Loading login page...");
  await page.goto(LOGIN_URL, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle").catch(() => {});

  // Screenshot the login page
  await page.screenshot({ path: path.join(ERRORS_DIR, "01_login_page.png"), fullPage: true });
  console.log("[1] Screenshot saved: errors/01_login_page.png");

  // Dump all form elements
  const formInfo = await page.evaluate(() => {
    const results = { forms: [], inputs: [], buttons: [], scripts: [] };

    // Forms
    document.querySelectorAll("form").forEach((f, i) => {
      results.forms.push({
        index: i,
        id: f.id,
        action: f.action,
        method: f.method,
        class: f.className,
        html: f.outerHTML.slice(0, 2000),
      });
    });

    // All inputs
    document.querySelectorAll("input, textarea, select").forEach((el) => {
      results.inputs.push({
        tag: el.tagName,
        type: el.type,
        name: el.name,
        id: el.id,
        placeholder: el.placeholder,
        value: el.value,
        class: el.className,
        visible: el.offsetParent !== null,
      });
    });

    // All buttons / clickable submit elements
    document.querySelectorAll("button, input[type='submit'], a.btn").forEach((el) => {
      results.buttons.push({
        tag: el.tagName,
        type: el.type,
        text: el.textContent.trim().slice(0, 100),
        id: el.id,
        class: el.className,
        onclick: el.getAttribute("onclick"),
      });
    });

    // Inline scripts that mention login/slogin
    document.querySelectorAll("script").forEach((s) => {
      const text = s.textContent;
      if (text && (text.includes("login") || text.includes("slogin") || text.includes("ajax"))) {
        results.scripts.push(text.trim().slice(0, 1500));
      }
    });

    return results;
  });

  console.log("\n=== FORMS ===");
  console.log(JSON.stringify(formInfo.forms, null, 2));

  console.log("\n=== INPUTS ===");
  console.log(JSON.stringify(formInfo.inputs, null, 2));

  console.log("\n=== BUTTONS ===");
  console.log(JSON.stringify(formInfo.buttons, null, 2));

  console.log("\n=== LOGIN-RELATED SCRIPTS ===");
  formInfo.scripts.forEach((s, i) => {
    console.log(`\n--- Script ${i + 1} ---`);
    console.log(s);
  });

  // Attempt to fill credentials
  if (username && password) {
    console.log("\n[2] Attempting to fill credentials...");

    // Try common selectors for the email/username field
    const fieldSelectors = [
      "input[name='username']",
      "input[name='email']",
      "input[name='emailaddress']",
      "input[type='email']",
      "input[type='text']",
      "#email",
      "#login_email",
    ];

    let filled = false;
    for (const sel of fieldSelectors) {
      try {
        const el = page.locator(sel).first();
        if (await el.isVisible({ timeout: 1_000 })) {
          await el.fill(username);
          console.log(`  Filled username into: ${sel}`);
          filled = true;
          break;
        }
      } catch {}
    }
    if (!filled) console.log("  [warn] Could not find username field");

    // Password field
    const passSelectors = [
      "input[name='password']",
      "input[type='password']",
      "#password",
    ];
    filled = false;
    for (const sel of passSelectors) {
      try {
        const el = page.locator(sel).first();
        if (await el.isVisible({ timeout: 1_000 })) {
          await el.fill(password);
          console.log(`  Filled password into: ${sel}`);
          filled = true;
          break;
        }
      } catch {}
    }
    if (!filled) console.log("  [warn] Could not find password field");

    await page.screenshot({ path: path.join(ERRORS_DIR, "02_credentials_filled.png"), fullPage: true });
    console.log("[2] Screenshot saved: errors/02_credentials_filled.png");

    // Try clicking submit
    console.log("\n[3] Attempting login submit...");
    const submitSelectors = [
      "button[type='submit']",
      "input[type='submit']",
      "button:has-text('Login')",
      "button:has-text('Sign In')",
      "a:has-text('Login')",
    ];

    for (const sel of submitSelectors) {
      try {
        const el = page.locator(sel).first();
        if (await el.isVisible({ timeout: 1_000 })) {
          console.log(`  Clicking: ${sel}`);
          await el.click();
          break;
        }
      } catch {}
    }

    // Wait and observe what happens
    await page.waitForTimeout(3_000);
    await page.screenshot({ path: path.join(ERRORS_DIR, "03_after_login_click.png"), fullPage: true });
    console.log("[3] Screenshot saved: errors/03_after_login_click.png");
    console.log(`[3] Current URL: ${page.url()}`);

    // Check for error messages
    const errorText = await page.evaluate(() => {
      const alerts = document.querySelectorAll(".alert, .error, .text-danger, [role='alert']");
      return [...alerts].map((el) => el.textContent.trim()).filter(Boolean);
    });
    if (errorText.length) {
      console.log("[3] Error messages on page:", errorText);
    }

    // Check if we landed on dashboard
    if (page.url().includes("index.php")) {
      console.log("[3] LOGIN SUCCESS — redirected to dashboard.");
      await page.screenshot({ path: path.join(ERRORS_DIR, "04_dashboard.png"), fullPage: true });
    } else {
      console.log("[3] Still on login page or other page. Check screenshots.");
    }

    // Dump the page HTML after login attempt for further inspection
    const postLoginHTML = await page.content();
    fs.writeFileSync(path.join(ERRORS_DIR, "post_login_page.html"), postLoginHTML);
    console.log("[3] Full HTML saved: errors/post_login_page.html");
  }

  // Keep browser open for 60 seconds so user can inspect
  console.log("\n[info] Browser will stay open for 60 seconds for manual inspection...");
  console.log("[info] Press Ctrl+C to close early.");
  await page.waitForTimeout(60_000);

  await browser.close();
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
