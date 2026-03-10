// index.js - No database version - Railway friendly
const { Client, GatewayIntentBits, EmbedBuilder, Colors, SlashCommandBuilder } = require('discord.js');
const axios = require('axios');
const { Connection, PublicKey } = require('@solana/web3.js');

// ────────────────────────────────────────────────
//               CONFIG - PUT YOUR SECRETS HERE
// ────────────────────────────────────────────────
const DISCORD_TOKEN         = process.env.DISCORD_TOKEN;
const USER_ID               = process.env.USER_ID;              // Your Discord user ID as string
const LTC_BLOCKCYPHER_TOKEN = process.env.LTC_BLOCKCYPHER_TOKEN || ''; // optional

// Hardcode your wallet addresses here (easiest way - survives redeploy)
const LTC_ADDRESSES = [  Lf8FD7Muy4e84EGWBLtdtYBMbm7BYdQQP5
  // 'Lxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
  // 'Mxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
  // Add as many as you want
];

const SOL_ADDRESSES = [
  // 'SoLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
  // 'AnotherSolAddressHerexxxxxxxxxxxxxxxxxxxxxxxxxxxx',
  // Add your Solana addresses
];

// If you want to start empty and add via command only → leave arrays empty
// const LTC_ADDRESSES = [];
// const SOL_ADDRESSES = [];

// ────────────────────────────────────────────────
//               STATE
// ────────────────────────────────────────────────
const lastLtcTxs = new Set();
const lastSolSigs = new Set();

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
//               LTC CHECK
// ────────────────────────────────────────────────
async function checkLtcTransactions() {
  if (LTC_ADDRESSES.length === 0) return;

  for (const address of LTC_ADDRESSES) {
    try {
      let url = `https://api.blockcypher.com/v1/ltc/main/addrs/${address}?limit=5`;
      if (LTC_BLOCKCYPHER_TOKEN) url += `&token=${LTC_BLOCKCYPHER_TOKEN}`;

      const { data } = await axios.get(url);
      const txs = data.txs || [];

      for (const tx of txs) {
        const txHash = tx.hash;
        if (lastLtcTxs.has(txHash)) continue;

        lastLtcTxs.add(txHash);
        const confs = tx.confirmations || 0;
        const valueLtc = (tx.total || 0) / 1e8;

        await sendNotification('LTC', txHash, confs, valueLtc, `https://blockchair.com/litecoin/transaction/${txHash}`);
      }
    } catch (err) {
      console.error(`LTC check failed for ${address}:`, err.message);
    }
  }
}

// ────────────────────────────────────────────────
//               SOL CHECK
// ────────────────────────────────────────────────
async function checkSolTransactions() {
  if (SOL_ADDRESSES.length === 0) return;

  const connection = new Connection('https://api.mainnet-beta.solana.com', 'confirmed');

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
        const roughConfs = Math.max(0, Math.floor((Date.now() - slotTime) / 1500));

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
      .setDescription(`Detected on ${coin} network`)
      .addFields(
        { name: 'TXID', value: `[${txid.slice(0,12)}...${txid.slice(-6)}](${url})`, inline: false },
        { name: 'Confirmations', value: `${confirmations}`, inline: true },
        { name: coin === 'LTC' ? 'Amount' : 'Fee', value: `${value.toFixed(6)} ${coin}`, inline: true }
      )
      .setColor(coin === 'LTC' ? Colors.Blue : Colors.Purple)
      .setThumbnail(coin === 'LTC'
        ? 'https://cryptologos.cc/logos/litecoin-ltc-logo.png'
        : 'https://cryptologos.cc/logos/solana-sol-logo.png')
      .setTimestamp()
      .setFooter({ text: 'PayPulse Monitor' });

    await user.send({ embeds: [embed] });
    console.log(`Sent ${coin} noti → ${txid.slice(0,12)}...`);
  } catch (err) {
    console.error('DM failed:', err.message);
  }
}

// ────────────────────────────────────────────────
//               POLLING (every 30s)
// ────────────────────────────────────────────────
setInterval(() => {
  checkLtcTransactions().catch(console.error);
  checkSolTransactions().catch(console.error);
}, 30000);

// ────────────────────────────────────────────────
//               ADD COMMAND
// ────────────────────────────────────────────────
client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;

  if (interaction.commandName === 'add-wallet') {
    const coin    = interaction.options.getString('coin', true);
    const address = interaction.options.getString('address', true).trim();

    if (!['LTC', 'SOL'].includes(coin)) {
      return interaction.reply({ content: 'Only LTC or SOL allowed.', ephemeral: true });
    }

    let targetArray = coin === 'LTC' ? LTC_ADDRESSES : SOL_ADDRESSES;

    if (targetArray.includes(address)) {
      return interaction.reply({ content: `Already monitoring this ${coin} address.`, ephemeral: true });
    }

    targetArray.push(address);
    await interaction.reply({ content: `✅ Now monitoring ${coin} address: \`${address}\`\n(Note: lost on restart)`, ephemeral: true });
    console.log(`Added ${coin} address: ${address}`);
  }
});

// ────────────────────────────────────────────────
//               STARTUP
// ────────────────────────────────────────────────
client.once('clientReady', async () => {
  console.log(`Logged in as ${client.user.tag}`);

  console.log(`Monitoring LTC: ${LTC_ADDRESSES.length} addresses`);
  console.log(`Monitoring SOL: ${SOL_ADDRESSES.length} addresses`);

  // Register slash command
  const cmd = new SlashCommandBuilder()
    .setName('add-wallet')
    .setDescription('Add LTC or SOL wallet to monitor (memory only)')
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
    console.log('/add-wallet registered (global)');
  } catch (err) {
    console.error('Command register failed:', err);
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
