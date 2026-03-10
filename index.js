// index.js
const { Client, GatewayIntentBits, EmbedBuilder, Colors, SlashCommandBuilder, PermissionFlagsBits } = require('discord.js');
const axios = require('axios');
const { Connection, PublicKey } = require('@solana/web3.js');
const { Pool } = require('pg');

// ────────────────────────────────────────────────
//               ENVIRONMENT VARIABLES
// ────────────────────────────────────────────────
const DISCORD_TOKEN           = process.env.DISCORD_TOKEN;
const USER_ID                 = process.env.USER_ID;                // Your Discord user ID (string)
const LTC_BLOCKCYPHER_TOKEN   = process.env.LTC_BLOCKCYPHER_TOKEN || ''; // optional
const SOL_RPC_URL             = 'https://api.mainnet-beta.solana.com';

// ────────────────────────────────────────────────
//               GLOBAL STATE
// ────────────────────────────────────────────────
let LTC_ADDRESSES = [];
let SOL_ADDRESSES = [];

const lastLtcTxs = new Set();
const lastSolSigs = new Set();

// ────────────────────────────────────────────────
//               POSTGRES POOL (Railway friendly)
// ────────────────────────────────────────────────
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.DATABASE_URL?.includes('railway.app')
    ? { rejectUnauthorized: false }   // Railway public URLs need this
    : undefined,
});

pool.on('connect', () => console.log('✅ Connected to PostgreSQL'));
pool.on('error', (err) => console.error('PostgreSQL pool error:', err));

// ────────────────────────────────────────────────
//               DISCORD CLIENT
// ────────────────────────────────────────────────
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.DirectMessages,
    GatewayIntentBits.MessageContent,
  ],
});

// ────────────────────────────────────────────────
//               DB HELPERS
// ────────────────────────────────────────────────
async function initDB() {
  try {
    const client = await pool.connect();
    await client.query(`
      CREATE TABLE IF NOT EXISTS wallets (
        id      SERIAL PRIMARY KEY,
        coin    TEXT NOT NULL,
        address TEXT NOT NULL UNIQUE
      );
    `);
    client.release();
    console.log('✅ Database table ready');
  } catch (err) {
    console.error('❌ Failed to initialize database:', err);
  }
}

async function loadAddresses() {
  try {
    const res = await pool.query('SELECT coin, address FROM wallets');
    LTC_ADDRESSES = res.rows.filter(r => r.coin === 'LTC').map(r => r.address);
    SOL_ADDRESSES = res.rows.filter(r => r.coin === 'SOL').map(r => r.address);
    console.log(`Loaded ${LTC_ADDRESSES.length} LTC and ${SOL_ADDRESSES.length} SOL addresses`);
  } catch (err) {
    console.error('❌ Failed to load addresses:', err);
  }
}

// ────────────────────────────────────────────────
//               LTC CHECKER
// ────────────────────────────────────────────────
async function checkLtcTransactions() {
  if (LTC_ADDRESSES.length === 0) return;

  for (const address of LTC_ADDRESSES) {
    try {
      const url = `https://api.blockcypher.com/v1/ltc/main/addrs/${address}?limit=5`;
      if (LTC_BLOCKCYPHER_TOKEN) url += `&token=${LTC_BLOCKCYPHER_TOKEN}`;

      const { data } = await axios.get(url);
      const txs = data.txs || [];

      for (const tx of txs) {
        const txHash = tx.hash;
        if (!lastLtcTxs.has(txHash)) {
          lastLtcTxs.add(txHash);
          const confs = tx.confirmations || 0;
          const valueLtc = (tx.total || 0) / 1e8; // satoshis → LTC
          await sendNotification('LTC', txHash, confs, valueLtc, `https://blockchair.com/litecoin/transaction/${txHash}`);
        }
      }
    } catch (err) {
      console.error(`LTC check failed for ${address}:`, err.message);
    }
  }
}

// ────────────────────────────────────────────────
//               SOL CHECKER
// ────────────────────────────────────────────────
async function checkSolTransactions() {
  if (SOL_ADDRESSES.length === 0) return;

  const connection = new Connection(SOL_RPC_URL, 'confirmed');

  for (const addrStr of SOL_ADDRESSES) {
    try {
      const pubkey = new PublicKey(addrStr);
      const signatures = await connection.getSignaturesForAddress(pubkey, { limit: 5 });

      for (const sigInfo of signatures) {
        const sig = sigInfo.signature;
        if (lastSolSigs.has(sig)) continue;

        lastSolSigs.add(sig);

        const tx = await connection.getParsedTransaction(sig, {
          maxSupportedTransactionVersion: 0,
        });

        if (!tx) continue;

        const slotTime = sigInfo.blockTime ? sigInfo.blockTime * 1000 : Date.now();
        const roughConfs = Math.max(0, Math.floor((Date.now() - slotTime) / 1500)); // rough

        const feeSol = tx.meta?.fee ? tx.meta.fee / 1_000_000_000 : 0;

        await sendNotification('SOL', sig, roughConfs, feeSol, `https://solscan.io/tx/${sig}`);
      }
    } catch (err) {
      console.error(`SOL check failed for ${addrStr}:`, err.message);
    }
  }
}

// ────────────────────────────────────────────────
//               NOTIFICATION
// ────────────────────────────────────────────────
async function sendNotification(coin, txid, confirmations, value, url) {
  try {
    const user = await client.users.fetch(USER_ID);
    const embed = new EmbedBuilder()
      .setTitle(`🔔 New ${coin} Transaction`)
      .setDescription(`Transaction detected on the ${coin} network
