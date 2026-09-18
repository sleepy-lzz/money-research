// Validate the offline manual only; never start the trading backend.
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const {pathToFileURL} = require('url');
const {chromium} = require('C:/Users/31227/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const root = process.cwd();
  const qa = path.join(root, 'tmp/pdfs/manual/qa');
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [], external = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.route(/^https?:/, route => {external.push(route.request().url()); return route.abort();});
    await page.goto(pathToFileURL(path.join(root,'output/pdf/Ashare_User_Guide.html')).href);
    assert.equal(await page.locator('section').count(), 24);
    assert.equal(await page.locator('aside a').count(), 24);
    assert.equal(await page.locator('figure img').count(), 12);
    assert.equal(await page.locator('figure img').evaluateAll(imgs => imgs.every(i => i.complete && i.naturalWidth > 0)), true);
    await page.screenshot({path:path.join(qa,'html-desktop.png')});
    await page.locator('aside a[href="#p13"]').click();
    assert.ok(page.url().endsWith('#p13'));
    await page.locator('#p13 figure img').click();
    assert.equal(await page.locator('dialog').evaluate(d => d.open), true);
    await page.getByRole('button',{name:'关闭大图'}).click();
    assert.equal(await page.locator('dialog').evaluate(d => d.open), false);
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(() => {document.documentElement.style.scrollBehavior='auto'; window.scrollTo(0,0);});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path:path.join(qa,'html-mobile.png')});
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    const result={sections:24,images:12,images_loaded:true,toc:true,zoom:true,mobile_overflow:false,external_requests:external,page_errors:errors};
    fs.writeFileSync(path.join(qa,'html-verification.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally {await browser.close();}
})().catch(e => {console.error(e);process.exit(1)});
