// Real-browser evidence navigation checks. Serves only the generated public demo on loopback.
// Requires Playwright and an installed Chromium; optional STAGES_CHROMIUM / STAGES_D3_PATH overrides.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../../docs/demo');
const types = { '.html': 'text/html', '.json': 'application/json', '.js': 'application/javascript' };
const server = http.createServer((req, res) => {
  const file = path.resolve(root, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname));
  if (!file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
  fs.readFile(file, (err, body) => {
    if (err) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', types[path.extname(file)] || 'text/plain');
    res.end(body);
  });
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/stages.html`;
  const browser = await chromium.launch({ headless: true, ...(process.env.STAGES_CHROMIUM ? { executablePath: process.env.STAGES_CHROMIUM } : {}) });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    if (process.env.STAGES_D3_PATH) {
      await context.route('**/d3.min.js', route => route.fulfill({ contentType: 'application/javascript', body: fs.readFileSync(process.env.STAGES_D3_PATH) }));
    }
    // Explorer URLs are tested as navigation destinations, without loading an external explorer UI.
    await context.route('https://explorer.solana.com/**', route => route.fulfill({ contentType: 'text/html', body: '<title>Evidence navigation destination</title>' }));
    const page = await context.newPage(), errors = [], requests = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', r => requests.push(r.url()));
    await page.goto(url);
    await page.evaluate(() => document.fonts.ready);
    await page.waitForLoadState('networkidle');
    await page.waitForSelector('#trade .chain circle');
    assert.equal(await page.locator('.screen').count(), 9);
    assert.equal(await page.locator('#trade .chain circle').count(), 600);
    assert.equal(await page.locator('#pay .chain rect').count(), 1000);
    assert.equal(await page.locator('#audit .chain rect.c').count(), 3959);
    const bindings = await page.evaluate(() => [...document.querySelectorAll('a[data-evidence]')].every(a => PAGE.evidence[a.dataset.evidence] && a.getAttribute('href')));
    assert(bindings, 'Every evidence anchor must resolve');
    const jump = async selector => {
      await page.locator(selector).first().evaluate(el => el.scrollIntoView({ behavior: 'instant', block: 'center' }));
      await page.waitForTimeout(100);
    };
    const attack = page.locator('a[data-evidence="attack:register-self"]').first();
    await jump('a[data-evidence="attack:register-self"]');
    const n = requests.length;
    await attack.hover();
    await page.waitForTimeout(750);
    assert(await page.locator('#audit-preview').isHidden(), 'Hover must wait a full second');
    await page.waitForTimeout(400);
    assert(await page.locator('#audit-preview').isVisible());
    assert.match(await page.locator('#audit-preview').innerText(), /ConstraintHasOne/);
    // Fonts can be loaded lazily when a new weight/glyph first appears in the card.
    assert.deepEqual(requests.slice(n).filter(u => !u.endsWith('/favicon.ico') && !u.startsWith('https://fonts.gstatic.com/')), [], 'Hover must not fetch evidence');
    await page.locator('#audit-preview h3').hover();
    await page.waitForTimeout(350);
    assert(await page.locator('#audit-preview').isVisible(), 'Moving into the card must keep it open');
    await page.screenshot({ path: '/tmp/stages-desktop.png' });
    await page.keyboard.press('Escape');
    assert(await page.locator('#audit-preview').isHidden());
    await page.mouse.move(2, 2);
    await attack.hover();
    await page.waitForTimeout(200);
    await page.mouse.move(2, 2);
    await page.waitForTimeout(1100);
    assert(await page.locator('#audit-preview').isHidden(), 'A brief hover must cancel');
    await attack.focus();
    await page.waitForTimeout(50);
    assert(await page.locator('#audit-preview').isVisible(), 'Keyboard focus opens evidence');
    await page.keyboard.press('Tab');
    assert(await page.locator('#audit-preview button').evaluate(el => el === document.activeElement), 'Tab enters the evidence card');
    const scroll = await page.evaluate(() => scrollY);
    await page.keyboard.press('PageDown');
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => scrollY), scroll, 'Popup control keys must not jump stages');
    await page.keyboard.press('Escape');
    assert(await attack.evaluate(el => el === document.activeElement), 'Closing the card restores link focus');
    const href = await attack.getAttribute('href');
    const opened = page.waitForEvent('popup');
    await attack.click();
    const popup = await opened;
    await popup.waitForLoadState();
    assert.equal(popup.url(), href, 'Click opens the exact transaction immediately');
    await popup.close();

    const sample = page.locator('#pay .chain a[data-evidence^="paid:"]').first();
    await jump('#pay .chain a[data-evidence^="paid:"]');
    await sample.focus();
    await page.waitForTimeout(50);
    assert.match(await page.locator('#audit-preview').innerText(), /Record quantity/);
    assert.match(await page.locator('#audit-preview').innerText(), /Measured cash/);
    await page.keyboard.press('Escape');
    await page.locator('#audit .chain a[data-evidence="check:0"]').focus();
    await page.waitForTimeout(50);
    assert.match(await page.locator('#audit-preview').innerText(), /Check ID/);
    await page.keyboard.press('Escape');

    await page.emulateMedia({ colorScheme: 'dark' });
    await page.setViewportSize({ width: 390, height: 844 });
    await jump('a[data-evidence="attack:register-self"]');
    await attack.focus();
    await page.waitForTimeout(50);
    const box = await page.locator('#audit-preview').boundingBox();
    assert(box.x >= 0 && box.y >= 0 && box.x + box.width <= 390 && box.y + box.height <= 844);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'No page overflow on mobile');
    await page.screenshot({ path: '/tmp/stages-mobile.png' });
    const source = await context.newPage();
    await source.goto(url.replace('stages.html', 'audit/source/ops/src/attacks.ts.html#L124'));
    assert.match(await source.textContent('header'), /SHA-256/);
    assert(await source.locator('#L124').count());
    await source.close();
    const fallback = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
    const plain = await fallback.newPage();
    await plain.goto(url);
    assert.equal(await plain.locator('.screen').count(), 9);
    assert((await plain.locator('.ref a').count()) > 0);
    assert.equal(await plain.locator('.val a').count(), 9);
    const review = await context.newPage();
    await review.goto(url.replace('stages.html', 'audit/review.html'));
    const hostileRun = JSON.parse(fs.readFileSync(path.join(root, 'data/devnet-hostile-attempts.json'), 'utf8'));
    assert.equal(await review.locator('tbody').first().locator('tr').count(), hostileRun.attempts.length);
    assert.equal(await review.locator('tbody').nth(1).locator('tr').count(), hostileRun.serviced.length);
    assert.deepEqual(errors, []);
    console.log('PASS: all chart bindings, 1-second hover/no evidence fetch, dismissal, keyboard, popup navigation, desktop/mobile dark, source lines, no-JS links, 33-probe review');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; }).finally(() => server.close());
