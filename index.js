// index.js - Clean no-DB version with debug + cool embeds
const { Client, GatewayIntentBits, EmbedBuilder, Colors, SlashCommandBuilder } = require('discord.js');
const axios = require('axios');
const { Connection, PublicKey } = require('@solana/web3.js');

// ────────────────────────────────────────────────
// CONFIG - FILL THESE IN RAILWAY VARIABLES OR HARDCODE
// ────────────────────────────────────────────────
const DISCORD_TOKEN         = process.env.DISCORD_TOKEN || 'YOUR_TOKEN_HERE';
const USER_ID               = process.env.USER_ID || 'YOUR_DISCORD_USER_ID_HERE'; // string, e.g. '123456789012345678'
const LTC_BLOCKCYPHER_TOKEN = process.env.LTC_BLOCKCYPHER_TOKEN || ''; // optional

// Hardcoded addresses (add yours here with quotes!)
let LTC_ADDRESSES = [
  // "Lf8FD7Muy4e84EGWBLtdtYBMbm7BYdQQP5",   // example LTC
  // "another-ltc-address",
];

let SOL_ADDRESSES = [
  // "SoLanaAddressHere111111111111111111111111111",   // example SOL
  // "another-sol-address",
];

// ────────────────────────────────────────────────
// STATE
// ────────────────────────────────────────────────
const seenLtcTxs = new Set();
const seenSolSigs = new Set();

// ────────────────────────────────────────────────
// CLIENT
// ────────────────────────────────────────────────
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.DirectMessages,
    GatewayIntentBits.MessageContent,
  ],
});

// ────────────────────────────────────────────────
// LTC POLLER
// ────────────────────────────────────────────────
async function checkLtc() {
  if (LTC_ADDRESSES.length === 0) return;

  for (const addr of LTC_ADDRESSES) {
    try {
      let url = `https://api.blockcypher.com/v1/ltc/main/addrs/${addr}?limit=5`;
      if (LTC_BLOCKCYPHER_TOKEN) url += `&token=${LTC_BLOCKCYPHER_TOKEN}`;

      const { data } = await axios.get(url);
      const txs = data.txs || [];

      for (const tx of txs) {
        if (seenLtcTxs.has(tx.hash)) continue;
        seenLtcTxs.add(tx.hash);

        const confs = tx.confirmations || 0;
        const amount = (tx.total || 0) / 1e8;

        await notify('LTC', tx.hash, confs, amount, `https://blockchair.com/litecoin/transaction/${tx.hash}`);
      }
    } catch (err) {
      console.error(`LTC poll error for ${addr}:`, err.message);
    }
  }
}

// ────────────────────────────────────────────────
// SOL POLLER
// ────────────────────────────────────────────────
async function checkSol() {
  if (SOL_ADDRESSES.length === 0) return;

  const conn = new Connection('https://api.mainnet-beta.solana.com', 'confirmed');

  for (const addr of SOL_ADDRESSES) {
    try {
      const pubkey = new PublicKey(addr);
      const sigs = await conn.getSignaturesForAddress(pubkey, { limit: 5 });

      for (const info of sigs) {
        if (seenSolSigs.has(info.signature)) continue;
        seenSolSigs.add(info.signature);

        const tx = await conn.getParsedTransaction(info.signature, { maxSupportedTransactionVersion: 0 });
        if (!tx) continue;

        const timeMs = (info.blockTime ?? Date.now() / 1000) * 1000;
        const confs = Math.max(0, Math.floor((Date.now() - timeMs) / 1500));

        const fee = tx.meta?.fee ? tx.meta.fee / 1e9 : 0;

        await notify('SOL', info.signature, confs, fee, `https://solscan.io/tx/${info.signature}`);
      }
    } catch (err) {
      console.error(`SOL poll error for ${addr}:`, err.message);
    }
  }
}

// ────────────────────────────────────────────────
// NOTIFY FUNCTION - with debug
// ────────────────────────────────────────────────
async function notify(coin, txid, confs, value, url) {
  try {
    const user = await client.users.fetch(USER_ID);
    console.log(`[NOTIFY] Attempting DM to ${USER_ID} for ${coin} tx ${txid.slice(0,10)}...`);

    const embed = new EmbedBuilder()
      .setTitle(`🔔 ${coin} Transaction Detected`)
      .setDescription(`New activity on one of your ${coin} addresses`)
      .setColor(coin === 'LTC' ? Colors.Blue : Colors.Purple)
      .addFields(
        { name: 'TXID',       value: `[${txid.slice(0,10)}...${txid.slice(-6)}](${url})`, inline: false },
        { name: 'Confirmations', value: confs.toString(), inline: true },
        { name: coin === 'LTC' ? 'Amount' : 'Fee', value: `${value.toFixed(6)} ${coin}`, inline: true },
      )
      .setThumbnail(coin === 'LTC'
        ? 'https://cryptologos.cc/logos/litecoin-ltc-logo.png'
        : 'https://cryptologos.cc/logos/solana-sol-logo.png')
      .setTimestamp()
      .setFooter({ text: 'PayPulse • Real-time alerts' });

    await user.send({ embeds: [embed] });
    console.log(`[SUCCESS] DM sent for ${coin} → ${txid.slice(0,10)}...`);
  } catch (err) {
    console.error('[DM ERROR] Failed to send to', USER_ID);
    console.error('Error details:', err);
    if (err.code === 50007) {
      console.error('→ Common cause: User privacy settings block DMs from this bot / server members OR no shared server');
    } else if (err.code) {
      console.error(`→ Discord error code: ${err.code}`);
    }
  }
}

// ────────────────────────────────────────────────
// POLLING
// ────────────────────────────────────────────────
setInterval(() => {
  checkLtc().catch(console.error);
  checkSol().catch(console.error);
}, 30000);

// ────────────────────────────────────────────────
// COMMANDS
// ────────────────────────────────────────────────
client.on('interactionCreate', async (interaction) => {
  if (!interaction.isChatInputCommand()) return;

  const { commandName } = interaction;

  if (commandName === 'add-wallet') {
    const coin = interaction.options.getString('coin', true);
    const addr = interaction.options.getString('address', true).trim();

    if (!['LTC', 'SOL'].includes(coin)) {
      return interaction.reply({ content: 'Only LTC or SOL allowed.', ephemeral: true });
    }

    const arr = coin === 'LTC' ? LTC_ADDRESSES : SOL_ADDRESSES;
    if (arr.includes(addr)) {
      return interaction.reply({ content: `Already monitoring this ${coin} address.`, ephemeral: true });
    }

    arr.push(addr);
    await interaction.reply({ content: `✅ Added ${coin} address: \`${addr}\` (memory only – lost on restart)`, ephemeral: true });
    console.log(`Added ${coin}: ${addr}`);
  }

  if (commandName === 'list') {
    const ltc = LTC_ADDRESSES.length ? LTC_ADDRESSES.join('\n') : 'None';
    const sol = SOL_ADDRESSES.length ? SOL_ADDRESSES.join('\n') : 'None';

    await interaction.reply({
      content: `**Monitored addresses**\n\n**LTC:**\n${ltc}\n\n**SOL:**\n${sol}`,
      ephemeral: true,
    });
  }
});

// ────────────────────────────────────────────────
// READY
// ────────────────────────────────────────────────
client.once('clientReady', () => {
  console.log(`Logged in as ${client.user.tag} | Monitoring ${LTC_ADDRESSES.length} LTC + ${SOL_ADDRESSES.length} SOL`);

  // Register commands
  const commands = [
    new SlashCommandBuilder()
      .setName('add-wallet')
      .setDescription('Add address to monitor (temporary)')
      .addStringOption(o => o.setName('coin').setDescription('LTC or SOL').setRequired(true)
        .addChoices({ name: 'LTC', value: 'LTC' }, { name: 'SOL', value: 'SOL' }))
      .addStringOption(o => o.setName('address').setDescription('Wallet address').setRequired(true)),

    new SlashCommandBuilder()
      .setName('list')
      .setDescription('Show currently monitored addresses'),
  ];

  client.application.commands.set(commands).catch(err => console.error('Command register failed:', err));

  // Initial poll
  checkLtc();
  checkSol();
});

// ────────────────────────────────────────────────
// START
// ────────────────────────────────────────────────
client.login(DISCORD_TOKEN).catch(err => {
  console.error('Login failed:', err);
  process.exit(1);
});
