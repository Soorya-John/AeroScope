const {test,expect}=require('@playwright/test');
test('real Copter SITL through browser connection configuration and mission workflows',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 page.on('dialog',d=>d.type()==='prompt'?d.accept(d.message().includes('UPLOAD')?'UPLOAD':''):d.accept());
 await page.goto('/#connect');await page.locator('#claim-control').click();await expect(page.locator('#owner-status')).toHaveText('You own vehicle controls');
 await page.locator('#environment').selectOption('sitl');await page.locator('#transport').selectOption('udp_listener');await page.locator('#connect-address').fill('127.0.0.1');await page.locator('#network-port').fill('14564');await page.locator('#workflow-connect').click();await expect(page.locator('#connection-label')).toHaveText('CONNECTED',{timeout:20000});
 await page.locator('a[data-page="configure"]').click();await page.locator('#config-download').click();await expect(page.locator('#config-progress')).toContainText('• complete',{timeout:30000});
 await page.locator('#config-search').fill('INS_GYRO_FILTER');const input=page.locator('[data-config-name="INS_GYRO_FILTER"]');await expect(input).toBeVisible();const before=await input.inputValue();await input.fill(String(Number(before)+1));await page.locator('#config-review').click();await expect(page.locator('#config-diff')).toContainText('INS_GYRO_FILTER');await page.locator('#config-apply').click();await expect(page.locator('#batch-status')).toContainText('VERIFIED',{timeout:20000});await expect(page.locator('#dirty-count')).toHaveText('No unsaved changes');
 await input.fill(before);await page.locator('#config-review').click();await page.locator('#config-apply').click();await expect(page.locator('#batch-status')).toContainText('VERIFIED',{timeout:20000});await expect(input).toHaveValue(before);
 await page.screenshot({path:'.tools/browser-sitl-config.png',fullPage:true});
 await page.locator('a[data-page="mission"]').click();
 const items=[{command:16,frame:6,latitude:-35.363261,longitude:149.165230,altitude:0},{command:22,frame:6,latitude:-35.363261,longitude:149.165230,altitude:10},{command:16,frame:6,latitude:-35.3631,longitude:149.1653,altitude:10},{command:20,frame:2,latitude:0,longitude:0,altitude:0}];
 await page.locator('#plan-import').setInputFiles({name:'mission.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({items}))});await expect(page.locator('#mission-rows tr')).toHaveCount(4);await page.locator('#mission-upload').click();await expect(page.locator('#mission-progress')).toContainText('SUCCEEDED',{timeout:20000});
 await page.locator('#mission-download').click();await expect(page.locator('#mission-progress')).toContainText('SUCCEEDED',{timeout:20000});await expect(page.locator('#mission-rows tr')).toHaveCount(4);await page.locator('#plan-compare').click();await expect(page.locator('#toast-stack')).toContainText('matches within');
 await page.screenshot({path:'.tools/browser-sitl-plan.png',fullPage:true});expect(errors).toEqual([]);
 await page.locator('a[data-page="connect"]').click();await page.locator('#workflow-disconnect').click();await expect(page.locator('#connection-label')).toHaveText('DISCONNECTED');
});
