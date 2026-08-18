import fs from "node:fs/promises";
import { chromium } from "playwright";

const url = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html";
const summaryPath = process.env.GITHUB_STEP_SUMMARY;
let browser;

try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ locale: "en-US" });
  const page = await context.newPage();
  const response = await page.goto(url, {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  await page.waitForTimeout(15_000);

  const frameReports = [];
  let matchedText = "";
  let matchedFrameUrl = "";
  for (const frame of page.frames()) {
    if (frame === page.mainFrame()) continue;
    try {
      const text = await frame.locator("body").innerText({ timeout: 10_000 });
      frameReports.push({ url: frame.url(), chars: text.length });
      if (
        !matchedText &&
        ["Ease", "No change", "Hike"].every((label) => text.includes(label))
      ) {
        matchedText = text;
        matchedFrameUrl = frame.url();
      }
    } catch {
      // Cross-origin or not-yet-rendered frames are not candidates.
    }
  }

  if (!matchedText) {
    throw new Error(
      "No rendered FedWatch probability frame found; status=" +
        (response?.status() ?? "unknown") +
        " frames=" +
        JSON.stringify(frameReports),
    );
  }

  const excerpt = matchedText.replace(/\s+/g, " ").slice(0, 2_000);
  const report = [
    "# Anonymous CME FedWatch probe",
    "",
    "- page_status: " + (response?.status() ?? "unknown"),
    "- frame_url: " + matchedFrameUrl,
    "- extraction_method: anonymous_playwright_visible_iframe_text",
    "- quality_mode: exploratory_web_rendered",
    "- strict_pit: false",
    "",
    "~~~text",
    excerpt,
    "~~~",
    "",
  ].join("\n");
  if (summaryPath) await fs.appendFile(summaryPath, report);
  console.log("CME_ANON_PROBE_SUCCESS");
  console.log(report);
} catch (error) {
  console.error("CME_ANON_PROBE_FAILED", error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
