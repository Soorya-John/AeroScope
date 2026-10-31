const {defineConfig}=require('@playwright/test');
module.exports=defineConfig({testDir:'./tests/browser',testMatch:'sitl.spec.js',workers:1,timeout:90000,use:{baseURL:'http://127.0.0.1:8878',channel:'msedge',headless:true,viewport:{width:1440,height:1000}},webServer:{command:'python tools/serve_sitl_browser.py',url:'http://127.0.0.1:8878/api/health',reuseExistingServer:false}});
