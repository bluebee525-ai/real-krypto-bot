// index.js
const { Client, GatewayIntentBits, EmbedBuilder, Colors, SlashCommandBuilder, PermissionFlagsBits } = require('discord.js');
const axios = require('axios');
const { Connection, PublicKey } = require('@solana/web3.js');
const { Pool } = require('pg');

// Configuration
const DISCORD_TOKEN = process.env.DISCORD_TOKEN; // Set in Railway env vars
const USER_ID = process.env.USER_ID; // Set in Railway env vars (your Discord user ID)
const LTC_BLOCKCYPHER_TOKEN = process.env.LTC_BLOCKCYPHER_TOKEN; // Optional, set in env
const SOL_RPC_URL = 'https://api.mainnet-beta.solana.com'; // Solana mainnet RPC
const DATABASE_URL = process.env.DATABASE_URL; // Set automatically when adding Postgres plugin on Railway

// Dynamic addresses loaded from DB
let LTC_ADDRESSES = [];
let SOL_ADDRESSES = [];

// Store last known TX hashes to detect new ones
const lastLtcTxs = new Set();
const lastSolSigs = new Set();

// Initialize Discord client
const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.DirectMessages, GatewayIntentBits.MessageContent],
});

// Postgres pool
const pool = new Pool({ connectionString: DATABASE_URL });

// Initialize DB
async function initDB() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS wallets (
      id SERIAL PRIMARY KEY,
      coin TEXT NOT NULL,
      address TEXT NOT NULL UNIQUE
    );
  `);
  console.log('DB initialized');
}

// Load addresses from DB
async function loadAddresses() {
  const res = await pool.query('SELECT coin, address FROM wallets');
  LTC_ADDRESSES = res.rows.filter(r => r.coin === 'LTC').map(r => r.address);
  SOL_ADDRESSES = res.rows.filter(r => r.coin === 'SOL').map(r => r.address);
  console.log(`Loaded ${LTC_ADDRESSES.length} LTC and ${SOL_ADDRESSES.length} SOL addresses`);
}

// Function to check LTC transactions
async function checkLtcTransactions() {
  if (LTC_ADDRESSES.length === 0) return;
  for (const address of LTC_ADDRESSES) {
    try {
      const response = await axios.get(`https://api.blockcypher.com/v1/ltc/main/addrs/${address}?token=${LTC_BLOCKCYPHER_TOKEN}&limit=5`);
      const txs = response.data.txs || [];
      for (const tx of txs) {
        const txHash = tx.hash;
        if (!lastLtcTxs.has(txHash)) {
          lastLtcTxs.add(txHash);
          const confirmations = tx.confirmations || 0;
          const value = tx.outputs.reduce((sum, out) => sum + (out.value || 0), 0) / 100000000; // Convert satoshis to LTC
          await sendNotification('LTC', txHash, confirmations, value, `https://blockchair.com/litecoin/transaction/${txHash}`);
        }
      }
    } catch (error) {
      console.error(`Error checking LTC for ${address}:`, error.message);
    }
  }
}

// Function to check SOL transactions
async function checkSolTransactions() {
  if (SOL_ADDRESSES.length === 0) return;
  const connection = new Connection(SOL_RPC_URL);
  for (const addressStr of SOL_ADDRESSES) {
    try {
      const publicKey = new PublicKey(addressStr);
      const signatures = await connection.getSignaturesForAddress(publicKey, { limit: 5 });
      for (const sigInfo of signatures) {
        const sig = sigInfo.signature;
        if (!lastSolSigs.has(sig)) {
          lastSolSigs.add(sig);
          const tx = await connection.getTransaction(sig, { commitment: 'confirmed' });
          if (tx && tx.meta) {
            // Rough confirmation estimate (~2s per slot)
            const confirmations = Math.floor((Date.now() - new Date(sigInfo.blockTime * 1000).getTime()) / 2000);
            const fee = tx.meta.fee / 1000000000; // Lamports to SOL
            await sendNotification('SOL', sig, confirmations, fee, `https://solscan.io/tx/${sig}`);
          }
        }
      }
    } catch (error) {
      console.error(`Error checking SOL for ${addressStr}:`, error.message);
    }
  }
}

// Function to send DM notification with embed
async function sendNotification(coin, txid, confirmations, value, explorerUrl) {
  try {
    const user = await client.users.fetch(USER_ID);
    const embed = new EmbedBuilder()
      .setTitle(`🔔 New ${coin} Transaction Detected!`)
      .setDescription(`A new transaction has been confirmed on the ${coin} blockchain.`)
      .addFields(
        { name: 'Transaction ID', value: `[${txid.slice(0, 20)}...](${explorerUrl})`, inline: false },
        { name: 'Confirmations', value: `${confirmations}`, inline: true },
        { name: 'Value/Fee', value: `${value} ${coin}`, inline: true }
      )
      .setColor(coin === 'LTC' ? Colors.Blue : Colors.Purple)
      .setThumbnail(coin === 'LTC' ? 'https://cryptologos.cc/logos/litecoin-ltc-logo.png' : 'https://cryptologos.cc/logos/solana-sol-logo.png')
      .setTimestamp()
      .setFooter({ text: 'Powered by Grok Bot' });

    await user.send({ embeds: [embed] });
    console.log(`Sent ${coin} notification for ${txid}`);
  } catch (error) {
    console.error('Error sending DM:', error.message);
  }
}

// Poll every 30 seconds (adjust as needed, but respect API limits)
setInterval(() => {
  checkLtcTransactions();
  checkSolTransactions();
}, 30000);

// Slash command handler
client.on('interactionCreate', async (interaction) => {
  if (!interaction.isChatInputCommand()) return;

  if (interaction.commandName === 'add-wallet') {
    const coin = interaction.options.getString('coin');
    const address = interaction.options.getString('address').trim();

    if (!['LTC', 'SOL'].includes(coin)) {
      return interaction.reply({ content: 'Invalid coin! Use LTC or SOL.', ephemeral: true });
    }

    // Basic validation (optional - expand as needed)
    if (coin === 'LTC' && !address.startsWith('L') && !address.startsWith('M')) {
      return interaction.reply({ content: 'Invalid LTC address format.', ephemeral: true });
    }
    if (coin === 'SOL' && address.length < 32) {
      return interaction.reply({ content: 'Invalid SOL address format.', ephemeral: true });
    }

    try {
      const result = await pool.query(
        'INSERT INTO wallets (coin, address) VALUES ($1, $2) ON CONFLICT (address) DO NOTHING RETURNING id;',
        [coin, address]
      );
      await loadAddresses(); // Reload addresses
      const status = result.rowCount > 0 ? 'Added new' : 'Already exists - updated';
      await interaction.reply({ content: `${status} ${coin} address: \`${address}\``, ephemeral: true });
    } catch (error) {
      console.error('DB error:', error);
      await interaction.reply({ content: 'Error adding address. Check logs.', ephemeral: true });
    }
  }
});

// Bot ready event
client.once('ready', async () => {
  console.log(`Logged in as ${client.user.tag}!`);
  await initDB();
  await loadAddresses();

  // Register slash command (runs on startup - safe for dev, but for prod, use a separate deploy script to avoid rate limits)
  const commands = [
    new SlashCommandBuilder()
      .setName('add-wallet')
      .setDescription('Add a wallet address to monitor')
      .addStringOption(option =>
        option.setName('coin')
          .setDescription('Coin type')
          .setRequired(true)
          .addChoices(
            { name: 'LTC', value: 'LTC' },
            { name: 'SOL', value: 'SOL' }
          ))
      .addStringOption(option =>
        option.setName('address')
          .setDescription('Wallet address')
          .setRequired(true)
      )
      .setDefaultMemberPermissions(PermissionFlagsBits.Administrator), // Optional: restrict to admins
  ];

  try {
    await client.application.commands.set(commands);
    console.log('Slash command registered');
  } catch (error) {
    console.error('Error registering command:', error);
  }

  // Initial check
  checkLtcTransactions();
  checkSolTransactions();
});

// Error handling for DB
pool.on('error', (err) => {
  console.error('Unexpected DB error:', err);
});

// Login
client.login(DISCORD_TOKEN);
