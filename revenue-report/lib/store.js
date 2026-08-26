'use strict';
/**
 * 配置与维护数据持久化（JSON 文件，首次运行时写入默认值）
 */
const fs = require('fs');
const path = require('path');
const mock = require('./mockdata');

const DATA_DIR = path.join(__dirname, '..', 'data');
const CFG_FILE = path.join(DATA_DIR, 'config.json');
const INPUTS_FILE = path.join(DATA_DIR, 'inputs.json');

const DEFAULT_CONFIG = {
  host: 'localhost',
  port: 5432,
  database: 'postgres',
  user: 'postgres',
  password: '1',
  schema: 'sdi_mes',
  table: 'sdi_th_wip_lot_transaction',
  fieldMap: {
    lot_name: 'lot_name',
    lot_type: 'lot_type',
    component_qty: 'component_qty',
    product_name: 'product_name',
    step_name: 'step_name',
    last_updated_time: 'last_updated_time',
    activity: 'activity',
  },
};

function ensureDir() {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
}

function readJson(file, def) {
  ensureDir();
  try {
    if (fs.existsSync(file)) return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) { /* 损坏时回退默认 */ }
  return JSON.parse(JSON.stringify(def));
}

function writeJson(file, obj) {
  ensureDir();
  fs.writeFileSync(file, JSON.stringify(obj, null, 2), 'utf8');
}

function getConfig() { return readJson(CFG_FILE, DEFAULT_CONFIG); }
function saveConfig(cfg) { writeJson(CFG_FILE, cfg); }

function getInputs() { return readJson(INPUTS_FILE, mock.inputs); }
function saveInputs(inputs) { writeJson(INPUTS_FILE, inputs); }

module.exports = { getConfig, saveConfig, getInputs, saveInputs, DEFAULT_CONFIG };
