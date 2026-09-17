'use strict';

const fs = require('fs');
const path = require('path');

const COLLECTIONS = ['services', 'versions', 'declarations', 'exemptions', 'reviews', 'publishes'];

/**
 * 极简 JSON 文件持久化存储。
 *
 * - 单进程写模型（Node 单线程事件循环内同步执行），每次变更后原子落盘（tmp + rename）。
 * - file 为 null 时为纯内存模式（用于测试）。
 * - 集合访问一律通过 store.data.<collection>，id 通过 store.nextId(<collection>) 分配。
 */
class Store {
  constructor(file) {
    this.file = file || null;
    this.data = { seq: {} };
    for (const c of COLLECTIONS) this.data[c] = [];
    if (this.file && fs.existsSync(this.file)) {
      const loaded = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      this.data = { seq: {}, ...loaded };
      for (const c of COLLECTIONS) this.data[c] = this.data[c] || [];
    }
  }

  nextId(collection) {
    this.data.seq[collection] = (this.data.seq[collection] || 0) + 1;
    return this.data.seq[collection];
  }

  save() {
    if (!this.file) return;
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2));
    fs.renameSync(tmp, this.file);
  }
}

module.exports = { Store };
