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
      .setDescription(`Transaction detected on the ${coin} network`)
      .addFields(
        { name: 'TXID',       value: `[${txid.slice(0,12)}...${txid.slice(-6)}](${url})`, inline: false },
        { name: 'Confirmations', value: `${confirmations}`, inline: true },
        { name: coin === 'LTC' ? 'Amount' : 'Fee', value: `${value.toFixed(6)} ${coin}`, inline: true }
      )
      .setColor(coin === 'LTC' ? Colors.Blue : Colors.Purple)
      .setThumbnail(coin === 'LTC'
        ? 'https://cryptologos.cc/logos/litecoin-ltc-logo.png'
        : 'https://cryptologos.cc/logos/solana-sol-logo.png?v=040')
      .setTimestamp()
      .setFooter({ text: 'PayPulse Crypto Monitor' });

    await user.send({ embeds: [embed] });
    console.log(`Sent ${coin} notification → ${txid.slice(0,12)}...`);
  } catch (err) {
    console.error('Failed to send DM:', err.message);
  }
}

// ────────────────────────────────────────────────
//               POLLING
// ────────────────────────────────────────────────
setInterval(() => {
  checkLtcTransactions().catch(console.error);
  checkSolTransactions().catch(console.error);
}, 30_000); // 30 seconds

// ────────────────────────────────────────────────
//               SLASH COMMANDS
// ────────────────────────────────────────────────
client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;

  if (interaction.commandName === 'add-wallet') {
    const coin    = interaction.options.getString('coin', true);
    const address = interaction.options.getString('address', true).trim();

    if (!['LTC','SOL'].includes(coin)) {
      return interaction.reply({ content: 'Only LTC or SOL allowed.', ephemeral: true });
    }

    try {
      const result = await pool.query(
        `INSERT INTO wallets (coin, address)
         VALUES ($1, $2)
         ON CONFLICT (address) DO NOTHING
         RETURNING id`,
        [coin, address]
      );

      await loadAddresses();

      const msg = result.rowCount > 0
        ? `✅ Added ${coin} address: \`${address}\``
        : `⚠️ Address already exists: \`${address}\``;

      await interaction.reply({ content: msg, ephemeral: true });
    } catch (err) {
      console.error('Add wallet error:', err);
      await interaction.reply({ content: 'Database error – check logs.', ephemeral: true });
    }
  }
});

// ────────────────────────────────────────────────
//               STARTUP
// ────────────────────────────────────────────────
client.once('clientReady', async () => {
  console.log(`Logged in as ${client.user.tag}`);

  // Debug env
  console.log('DATABASE_URL present:', !!process.env.DATABASE_URL);
  if (process.env.DATABASE_URL) {
    console.log('DATABASE_URL starts with:', process.env.DATABASE_URL.substring(0, 25) + '...');
  }

  await initDB();
  await loadAddresses();

  // Register slash command (global)
  const cmd = new SlashCommandBuilder()
    .setName('add-wallet')
    .setDescription('Add a crypto wallet to monitor')
    .addStringOption(opt =>
      opt.setName('coin')
        .setDescription('LTC or SOL')
        .setRequired(true)
        .addChoices({name:'LTC', value:'LTC'}, {name:'SOL', value:'SOL'})
    )
    .addStringOption(opt =>
      opt.setName('address')
        .setDescription('Wallet address')
        .setRequired(true)
    );

  try {
    await client.application.commands.create(cmd.toJSON());
    console.log('Slash command /add-wallet registered (global)');
  } catch (err) {
    console.error('Failed to register command:', err);
  }

  // First check
  checkLtcTransactions();
  checkSolTransactions();
});

// ────────────────────────────────────────────────
//               LOGIN
// ────────────────────────────────────────────────
client.login(DISCORD_TOKEN).catch(err => {
  console.error('Login failed:', err);
  process.exit(1);
});
