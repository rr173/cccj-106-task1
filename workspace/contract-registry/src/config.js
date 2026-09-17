'use strict';

const path = require('path');

module.exports = {
  port: parseInt(process.env.PORT || '8000', 10),
  // JSON 持久化文件位置；容器内挂载卷到 /data 即可持久化
  dataFile: process.env.DATA_FILE || path.join(process.cwd(), 'data', 'registry.json'),
  // 紧急豁免最长有效期（天）。豁免不允许永久存在。
  maxExemptionDays: parseInt(process.env.MAX_EXEMPTION_DAYS || '30', 10),
};
