'use strict';

const { createApp } = require('./app');
const { Store } = require('./store');
const config = require('./config');

const store = new Store(config.dataFile);
const app = createApp({ store, maxExemptionDays: config.maxExemptionDays });

app.listen(config.port, () => {
  console.log(`contract-registry listening on :${config.port}`);
  console.log(`data file: ${config.dataFile}`);
});
