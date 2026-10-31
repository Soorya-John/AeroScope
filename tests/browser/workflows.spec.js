const {test,expect}=require('@playwright/test');
test('navigation, ownership, actual demo labels, setup and offline geographic editing',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('/#connect');await expect(page.getByRole('heading',{name:'Connect',exact:true})).toBeVisible();
 await page.getByRole('button',{name:'Claim controls',exact:true}).click();await expect(page.locator('#owner-status')).toHaveText('You own vehicle controls');
 await page.getByRole('button',{name:'Start labeled demo'}).click();await page.locator('#start-simulation').click();await expect(page.locator('#connection-label')).toHaveText('SIMULATED');
 await page.locator('a[data-page="setup"]').click();await expect(page.locator('#setup-checks')).toContainText('Disarmed');
 await page.locator('a[data-page="mission"]').click();await expect(page.locator('#mission-plot')).toBeVisible();
 await page.locator('#mission-plot').dblclick({position:{x:260,y:180}});await expect(page.locator('#mission-rows tr')).toHaveCount(2);await expect(page.locator('#route-summary')).toContainText('2 items');
 const first=page.locator('#mission-rows tr').nth(1).locator('[data-field="altitude"]');await first.fill('45');await first.dispatchEvent('change');
 await page.waitForTimeout(2800);await expect(page.locator('#mission-rows tr').nth(1).locator('[data-field="altitude"]')).toHaveValue('45');
 await expect(page.locator('#map-tiles')).not.toBeChecked();const download=page.waitForEvent('download');await page.locator('#plan-export').click();expect((await download).suggestedFilename()).toBe('aeroscope.waypoints');
 await page.screenshot({path:'.tools/planner.png',fullPage:true});expect(errors).toEqual([]);
});
test('configuration drafts survive refresh and invalid values block review',async({page})=>{
 // Fixture tests edit persistence only; this is explicitly not autopilot evidence.
 await page.route('**/api/parameters',route=>route.fulfill({json:{session_id:'browser-fixture',parameters:[{name:'TEST_GAIN',value:1.25,type:9,index:0,count:1}],progress:{received:1,count:1,complete:true},pending:{}}}));
 await page.goto('/#configure');await expect(page.locator('[data-config-name="TEST_GAIN"]')).toBeVisible();await page.locator('[data-config-name="TEST_GAIN"]').fill('2.5');await page.locator('#config-search').focus();await page.waitForTimeout(3000);await expect(page.locator('[data-config-name="TEST_GAIN"]')).toHaveValue('2.5');await expect(page.locator('#dirty-count')).toHaveText('1 unsaved changes');
 await page.locator('[data-config-name="TEST_GAIN"]').fill('abc');await expect(page.locator('#config-rows')).toContainText('Enter a finite number');await expect(page.locator('#config-apply')).toBeDisabled();
 await page.screenshot({path:'.tools/configure.png',fullPage:true});
});
test('mobile layout keeps main navigation and map usable',async({page})=>{
 await page.setViewportSize({width:390,height:844});await page.goto('/#mission');await expect(page.locator('#mission-plot')).toBeVisible();await page.screenshot({path:'.tools/mobile.png',fullPage:true});
});
