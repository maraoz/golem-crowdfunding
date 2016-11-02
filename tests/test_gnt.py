import math
import os
import random
import unittest
from collections import deque
from contextlib import contextmanager
from random import randint
from os import urandom

import re
from ethereum import abi, tester
from ethereum.config import default_config
from ethereum.keys import sha3
from ethereum.tester import TransactionFailed, ContractCreationFailed
from ethereum.utils import denoms, privtoaddr, to_string, parse_int_or_hex
from rlp.utils import decode_hex

tester.serpent = True  # tester tries to load serpent module, prevent that.

# GNT contract bytecode (used to create the contract) and ABI.
# This is procudes by solidity compiler from Token.sol file.
# You can use Solidity Browser
# https://ethereum.github.io/browser-solidity/#version=soljson-v0.4.2+commit.af6afb04.js&optimize=true
# to work on and update the Token.

GNT_INIT = decode_hex(open('tests/GolemNetworkToken.bin', 'r').read().rstrip())
GNT_ABI = open('tests/GolemNetworkToken.abi', 'r').read()

MIGRATION_INIT = decode_hex(open('tests/MigrationAgent.bin', 'r').read().rstrip())
MIGRATION_ABI = open('tests/MigrationAgent.abi', 'r').read()

TARGET_INIT = decode_hex(open('tests/GNTTargetToken.bin', 'r').read().rstrip())
TARGET_ABI = open('tests/GNTTargetToken.abi', 'r').read()

ALLOC_INIT = decode_hex(open('tests/GNTAllocation.bin', 'r').read().rstrip())
ALLOC_ABI = open('tests/GNTAllocation.abi', 'r').read()

WALLET_INIT = decode_hex(open('tests/BadWallet.bin', 'r').read().rstrip())
WALLET_ABI = open('tests/BadWallet.abi', 'r').read()

GNT_CONTRACT_PATH = os.path.join('contracts', 'Token.sol')
ALLOC_CONTRACT_PATH = os.path.join('contracts', 'GNTAllocation.sol')

IMPORT_TOKEN_REGEX = '(import "\.\/Token\.sol";).*'
IMPORT_ALLOC_REGEX = '(import "\.\/GNTAllocation\.sol";).*'
DEV_ADDR_REGEX = "\s*allocations\[([a-zA-Z0-9]+)\].*"

gwei = 10 ** 9

tester.gas_limit = int(1.9 * 10 ** 6)
tester.gas_price = int(22.5 * gwei)


@contextmanager
def work_dir_context(file_path):
    cwd = os.getcwd()
    file_name = os.path.basename(file_path)
    rel_dir = os.path.dirname(file_path) or '.'
    dir_name = os.path.abspath(rel_dir)

    os.chdir(dir_name)
    yield file_name
    os.chdir(cwd)


class ContractHelper(object):
    """
    Tools for replacing strings in contract (regex). Default behaviour: replace developer addresses
    """

    def __init__(self, contract_path, regex=None):
        if not regex:
            regex = DEV_ADDR_REGEX

        self.regex = re.compile(regex)
        self.contract_path = contract_path

        with work_dir_context(contract_path) as file_name:
            self.source = open(file_name).read().rstrip()

    def findall(self, regex=None):
        return self._re(regex).findall(self.source)

    def sub(self, replacements, regex=None):
        i = [-1]

        def replace(m):
            i[0] += 1
            if i[0] < len(replacements):
                return m.group(0).replace(m.group(1), replacements[i[0]])
            return m.group(0)

        self.source = self._re(regex).sub(replace, self.source)

    def _re(self, regex):
        if regex:
            return re.compile(regex)
        return self.regex

    @staticmethod
    def dev_address(addr):
        return '0x' + addr.encode('hex')


def deploy_gnt(state, factory, dev_addresses, start, end, creator_idx=9):
    alloc_helper = ContractHelper(ALLOC_CONTRACT_PATH)
    # remove import
    alloc_helper.sub([''], regex=IMPORT_TOKEN_REGEX)
    # replace dev addresses
    alloc_helper.sub(dev_addresses)

    # replace import with contract source
    gnt_helper = ContractHelper(GNT_CONTRACT_PATH, regex=IMPORT_ALLOC_REGEX)
    gnt_helper.sub([alloc_helper.source])

    gas_before = state.block.gas_used
    starting_block = state.block.number

    with work_dir_context(gnt_helper.contract_path):
        contract = state.abi_contract(gnt_helper.source,
                                      language='solidity',
                                      sender=tester.keys[creator_idx],
                                      constructor_parameters=(factory, factory,
                                                              starting_block + start,
                                                              starting_block + end))

    return contract, contract.address, state.block.gas_used - gas_before


class GNTCrowdfundingTest(unittest.TestCase):

    # Test account monitor.
    # The ethereum.tester predefines 10 Ethereum accounts
    # (tester.accounts, tester.keys).
    class Monitor:
        def __init__(self, state, account_idx, value=0):
            self.addr = tester.accounts[account_idx]
            self.key = tester.keys[account_idx]
            self.state = state
            self.value = value
            self.initial = state.block.get_balance(self.addr)
            assert self.initial > 0
            assert self.addr != state.block.coinbase

        def gas(self):
            b = self.state.block.get_balance(self.addr)
            total = self.initial - b
            g = (total - self.value) / tester.gas_price
            return g

    def monitor(self, addr, value=0):
        return self.Monitor(self.state, addr, value)

    class EventListener:
        def __init__(self, contract, state):
            self.contract = contract
            self.state = state
            self.events = deque()

        def hook(self):
            self.state.block.log_listeners.append(self._listen)

        def unhook(self):
            listeners = self.state.block.log_listeners
            if self._listen in listeners:
                listeners.remove(self._listen)

        def event(self, event_type, **params):
            if self.events:
                event = self.events.popleft()  # FIFO
                type_matches = event["_event_type"] == event_type
                return type_matches and all([event.get(n) == v for n, v in params.items()])

        def _listen(self, event):
            self.events.append(self.contract.translator.listen(event))

    @contextmanager
    def event_listener(self, abi_contract, state):
        listener = self.EventListener(abi_contract, state)
        listener.hook()
        yield listener
        listener.unhook()

    def setUp(self):
        self.state = tester.state()
        self.starting_block = default_config.get('ANTI_DOS_FORK_BLKNUM') + 1
        self.state.block.number = self.starting_block

    def deploy_contract(self, founder, start, end,
                        creator_idx=9, migration_master=None):
        if migration_master is None:
            migration_master = founder
        owner = self.monitor(creator_idx)
        t = abi.ContractTranslator(GNT_ABI)
        args = t.encode_constructor_arguments((founder, migration_master,
                                               self.starting_block + start,
                                               self.starting_block + end))
        addr = self.state.evm(GNT_INIT + args,
                              sender=owner.key)
        self.c = tester.ABIContract(self.state, GNT_ABI, addr)
        return addr, owner.gas()

    def deploy_wallet(self, _founder, creator_idx=9):
        assert not hasattr(self, 'c')
        owner = self.monitor(creator_idx)
        t = abi.ContractTranslator(WALLET_ABI)
        args = t.encode_constructor_arguments([])
        addr = self.state.evm(WALLET_INIT + args,
                              sender=owner.key)
        self.wallet = tester.ABIContract(self.state, WALLET_ABI, addr)

        return addr, owner.gas()

    def deploy_contract_on_wallet(self, wallet_addr, start, end):
        c_addr = self.wallet.deploy_contract(wallet_addr,
                                             self.starting_block + start,
                                             self.starting_block + end)
        self.c = tester.ABIContract(self.state, GNT_ABI, c_addr)
        return c_addr

    def deploy_migration_contract(self, source_contract, creator_idx=9):
        owner = self.monitor(creator_idx)
        t = abi.ContractTranslator(MIGRATION_ABI)
        args = t.encode_constructor_arguments([source_contract])
        addr = self.state.evm(MIGRATION_INIT + args,
                              sender=owner.key)
        self.m = tester.ABIContract(self.state, MIGRATION_ABI, addr)
        return addr, owner.gas()

    def deploy_target_contract(self, migration_contract, creator_idx=9):
        owner = self.monitor(creator_idx)
        t = abi.ContractTranslator(TARGET_ABI)
        args = t.encode_constructor_arguments([migration_contract])
        addr = self.state.evm(TARGET_INIT + args,
                              sender=owner.key)
        self.t = tester.ABIContract(self.state, TARGET_ABI, addr)
        return addr, owner.gas()

    def deploy_contract_and_accounts(self, n_devs):
        dev_keys = []
        dev_accounts = []

        # create developer accounts and keys in fashion of testers
        for account_number in range(n_devs):
            dev_keys.append(sha3('dev' + to_string(account_number)))
            dev_accounts.append(privtoaddr(dev_keys[-1]))

        # developer balances
        block = self.state.block

        for i in range(n_devs):
            addr, data = dev_accounts[i], {'wei': 10 ** 24}
            if len(addr) == 40:
                addr = decode_hex(addr)
            assert len(addr) == 20
            block.set_balance(addr, parse_int_or_hex(data['wei']))

        block.commit_state()
        block.state.db.commit()

        dev_addresses = [ContractHelper.dev_address(a) for a in dev_accounts]

        # deploy the gnt contract with updated developer accounts
        contract, _, _ = deploy_gnt(self.state, tester.accounts[9], dev_addresses, 2, 2)
        allocation = tester.ABIContract(self.state, ALLOC_ABI, contract.lockedAllocation())

        return contract, allocation, dev_keys, dev_accounts

    def contract_balance(self):
        return self.state.block.get_balance(self.c.address)

    def balance_of(self, addr_idx):
        return self.c.balanceOf(tester.accounts[addr_idx])

    def transfer(self, sender, to, value):
        return self.c.transfer(to, value, sender=sender)

    def test_initial_balance(self):
        founder = tester.accounts[8]
        self.deploy_contract(founder, 5, 105)
        assert self.balance_of(8) == 0

    def test_deployment(self):
        founder = tester.accounts[2]
        c, g = self.deploy_contract(founder, 5, 105)
        assert len(c) == 20
        assert g <= 1557081
        assert self.contract_balance() == 0
        assert decode_hex(self.c.golemFactory()) == founder
        assert not self.c.fundingActive()

    def test_gas_for_create(self):
        self.state.block.coinbase = urandom(20)
        addr, _ = self.deploy_contract(urandom(20), 0, 100)
        costs = []
        for i, k in enumerate(tester.keys):
            v = random.randrange(1 * denoms.ether, 82000 * denoms.ether)
            m = self.monitor(i, v)
            self.state.send(k, addr, v)
            costs.append(m.gas())
        print(costs)
        assert max(costs) == 64396
        assert min(costs) == 64396 - 15000

    def test_gas_for_transfer(self):
        addr, _ = self.deploy_contract(urandom(20), 0, 1)
        for i, k in enumerate(tester.keys):
            v = random.randrange(15000 * denoms.ether, 82000 * denoms.ether)
            self.state.send(k, addr, v)
        self.state.mine(2)
        self.c.finalize()
        self.state.mine()
        self.state.block.coinbase = urandom(20)
        costs = []
        for i, k in enumerate(tester.keys):
            v = random.randrange(1, 15000000 * denoms.ether)
            m = self.monitor(i)
            self.c.transfer(urandom(20), v, sender=k)
            costs.append(m.gas())
        print(costs)
        assert max(costs) <= 51942
        assert min(costs) >= 51342

    def test_gas_for_migrate_all(self):
        factory_key = urandom(32)
        addr, _ = self.deploy_contract(privtoaddr(factory_key), 0, 1)
        for i, k in enumerate(tester.keys):
            v = random.randrange(15000 * denoms.ether, 82000 * denoms.ether)
            self.state.send(k, addr, v)
        self.state.mine(2)
        self.c.finalize()
        m_addr, _ = self.deploy_migration_contract(addr)
        t_addr, _ = self.deploy_target_contract(m_addr)
        self.c.setMigrationAgent(m_addr, sender=factory_key)
        self.m.setTargetToken(t_addr, sender=tester.k9)
        self.state.mine()
        self.state.block.coinbase = urandom(20)
        costs = []
        for i, k in enumerate(tester.keys):
            b = self.c.balanceOf(tester.accounts[i])
            m = self.monitor(i)
            self.c.migrate(b, sender=k)
            costs.append(m.gas())
        print(costs)
        assert max(costs) <= 98274
        assert min(costs) >= 56037

    def test_gas_for_migrate_half(self):
        factory_key = urandom(32)
        addr, _ = self.deploy_contract(privtoaddr(factory_key), 0, 1)
        for i, k in enumerate(tester.keys):
            v = random.randrange(15000 * denoms.ether, 82000 * denoms.ether)
            self.state.send(k, addr, v)
        self.state.mine(2)
        self.c.finalize()
        m_addr, _ = self.deploy_migration_contract(addr)
        t_addr, _ = self.deploy_target_contract(m_addr)
        self.c.setMigrationAgent(m_addr, sender=factory_key)
        self.m.setTargetToken(t_addr, sender=tester.k9)
        self.state.mine()
        self.state.block.coinbase = urandom(20)
        costs = []
        for i, k in enumerate(tester.keys):
            b = self.c.balanceOf(tester.accounts[i])
            m = self.monitor(i)
            self.c.migrate(b / 2, sender=k)
            costs.append(m.gas())
        print(costs)
        assert max(costs) <= 113274
        assert min(costs) >= 71037

    def test_gas_for_set_migration_agent_and_master(self):
        factory_key = urandom(32)
        addr, _ = self.deploy_contract(privtoaddr(factory_key), 0, 1)
        for i, k in enumerate(tester.keys):
            v = random.randrange(15000 * denoms.ether, 82000 * denoms.ether)
            self.state.send(k, addr, v)
        self.state.mine(2)
        self.c.finalize()
        m_addr, _ = self.deploy_migration_contract(addr)
        self.state.mine()

        lg = self.state.block.gas_used
        self.c.setMigrationAgent(m_addr, sender=factory_key)
        g = self.state.block.gas_used - lg
        assert g == 44093

        lg = self.state.block.gas_used
        self.c.setMigrationMaster(m_addr, sender=factory_key)
        g = self.state.block.gas_used - lg
        assert g == 28392

    def test_gas_for_refund(self):
        addr, _ = self.deploy_contract(urandom(20), 0, 1)
        for i, k in enumerate(tester.keys):
            v = random.randrange(1 * denoms.ether, 15000 * denoms.ether)
            self.state.send(k, addr, v)
        self.state.mine(2)
        self.state.block.coinbase = urandom(20)
        costs = []
        for i, k in enumerate(tester.keys):
            b = self.c.balanceOf(tester.accounts[i])
            m = self.monitor(i, -(b // 1000))
            self.c.refund(sender=k)
            costs.append(m.gas())
        print(costs)
        assert max(costs) <= 26936
        assert min(costs) >= 20263

    def test_gas_for_finalize(self):
        addr, _ = self.deploy_contract(urandom(20), 0, 1)
        for i, k in enumerate(tester.keys):
            v = random.randrange(15000 * denoms.ether, 82000 * denoms.ether)
            self.state.send(k, addr, v)
        self.state.mine(2)
        self.state.block.coinbase = urandom(20)
        m = self.monitor(0)
        self.c.finalize(sender=tester.k0)
        g = m.gas()
        assert g == 88272

    def test_gas_for_total_supply(self):
        addr, _ = self.deploy_contract(urandom(20), 0, 1)
        lg = self.state.block.gas_used
        self.c.totalSupply()
        g = self.state.block.gas_used - lg
        assert g == 21691

    def test_gas_for_balance_of(self):
        addr, _ = self.deploy_contract(urandom(20), 0, 1)
        for i, k in enumerate(tester.keys):
            v = random.randrange(1 * denoms.ether, 15000 * denoms.ether)
            self.state.send(k, addr, v)
        self.state.mine(2)
        lg = self.state.block.gas_used
        self.c.balanceOf(tester.accounts[0])
        g = self.state.block.gas_used - lg
        assert g == 23411

    def test_gas_for_funding_active(self):
        addr, _ = self.deploy_contract(urandom(20), 2, 2)

        self.state.mine(1)
        lg = self.state.block.gas_used
        self.c.fundingActive()
        g = self.state.block.gas_used - lg
        assert g == 22027

        self.state.mine(1)
        lg = self.state.block.gas_used
        self.c.fundingActive()
        g = self.state.block.gas_used - lg
        assert g == 22455

        self.state.mine(1)
        lg = self.state.block.gas_used
        self.c.fundingActive()
        g = self.state.block.gas_used - lg
        assert g == 22237

    def test_gas_for_number_of_tokens_left(self):
        addr, _ = self.deploy_contract(urandom(20), 2, 2)

        self.state.mine(1)
        lg = self.state.block.gas_used
        self.c.numberOfTokensLeft()
        g = self.state.block.gas_used - lg
        assert g == 22214

        self.state.mine(1)
        lg = self.state.block.gas_used
        self.c.numberOfTokensLeft()
        g = self.state.block.gas_used - lg
        assert g == 22214

        self.state.mine(1)
        lg = self.state.block.gas_used
        self.c.numberOfTokensLeft()
        g = self.state.block.gas_used - lg
        assert g == 22007

    def test_gas_for_finalized(self):
        addr, _ = self.deploy_contract(urandom(20), 1, 1)
        self.state.mine(1)
        lg = self.state.block.gas_used
        self.c.finalized()
        g = self.state.block.gas_used - lg
        assert g == 22069

    def test_transfer_enabled_after_end_block(self):
        founder = tester.accounts[4]
        addr, _ = self.deploy_contract(founder, 3, 13)
        assert self.state.block.number == self.starting_block
        assert not self.c.finalized()
        for _ in range(13):
            self.state.mine()
            assert not self.c.finalized()
        assert self.state.block.number == self.starting_block + 13

        # ensure min funding met
        self.state.send(tester.keys[1], addr, self.c.tokenCreationMin() / self.c.tokenCreationRate())
        self.state.mine()
        self.c.finalize()

        for _ in range(11):
            self.state.mine()
            assert self.c.finalized()

    def test_transfer_enabled_after_max_fund_reached(self):
        founder = tester.accounts[2]
        addr, _ = self.deploy_contract(founder, 3, 7)
        assert not self.c.finalized()
        for _ in range(3):
            self.state.mine()
            assert not self.c.finalized()

        assert self.state.block.number == self.starting_block + 3
        self.state.send(tester.keys[0], addr, 11)
        assert not self.c.finalized()
        self.state.send(tester.keys[1], addr, self.c.tokenCreationCap() / self.c.tokenCreationRate() - 11)
        assert not self.c.finalized()
        for _ in range(8):
            self.state.mine()
            assert not self.c.finalized()
        # Transfer is enabled after the funding is finalized.
        self.c.finalize(sender=tester.k5)
        assert self.c.finalized()

    def test_total_supply(self):
        founder = tester.accounts[7]
        addr, _ = self.deploy_contract(founder, 2, 4)
        assert self.c.totalSupply() == 0
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.keys[3], addr, 6611)
        assert self.c.totalSupply() == 0
        self.state.mine(2)
        assert self.c.totalSupply() == 0
        self.state.send(tester.keys[3], addr, 6611)
        assert self.c.totalSupply() == 6611000
        self.state.send(tester.keys[0], addr, 389)
        assert self.c.totalSupply() == 7000000
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.keys[0], addr, 0)
        assert self.c.totalSupply() == 7000000
        self.state.send(tester.keys[7], addr, self.c.tokenCreationMin() / self.c.tokenCreationRate())
        assert self.c.totalSupply() == 7000000 + self.c.tokenCreationMin()
        # mine past funding period
        self.state.mine(3)
        assert self.c.totalSupply() == 7000000 + self.c.tokenCreationMin()
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.keys[7], addr, 10)
        supplyBeforeEndowment = self.c.totalSupply()
        assert supplyBeforeEndowment == 7000000 + self.c.tokenCreationMin()
        self.c.finalize(sender=tester.keys[7])
        supplyAfterEndowment = self.c.totalSupply()
        endowmentPercent = (supplyAfterEndowment - supplyBeforeEndowment) \
            / float(supplyAfterEndowment)
        epsilon = 0.0001
        assert endowmentPercent < 0.18 + epsilon
        assert endowmentPercent > 0.18 - epsilon
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.keys[1], addr, 10)
        assert self.c.totalSupply() == supplyAfterEndowment

    def test_payable_period(self):
        c_addr, _ = self.deploy_contract(tester.a0, 2, 2)
        value = 3 * denoms.ether

        # before funding
        self.state.mine(1)
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k1, c_addr, value)

        # during funding
        self.state.mine(1)
        self.state.send(tester.k1, c_addr, value)

        # after funding
        self.state.mine(1)
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k1, c_addr, value)

    def test_payable_amounts(self):
        c_addr, _ = self.deploy_contract(tester.a0, 1, 1)
        value = 3 * denoms.ether

        self.state.mine(1)

        tokens_max = self.c.numberOfTokensLeft()

        # invalid value
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k1, c_addr, 0)

        # create 3 ether (value) worth of tokens for k1
        self.state.send(tester.k1, c_addr, value)
        numTokensCreated = value * self.c.tokenCreationRate()
        assert self.c.numberOfTokensLeft() == tokens_max - numTokensCreated
        assert self.c.totalSupply() == numTokensCreated

        # create 3 ether (value) worth of tokens for k2
        self.state.send(tester.k2, c_addr, value)
        assert self.c.numberOfTokensLeft() == tokens_max - 2 * numTokensCreated
        assert self.c.totalSupply() == 2 * numTokensCreated

        # issue remaining tokens, except 3 * "value" worth of tokens
        value_max = tokens_max / self.c.tokenCreationRate()
        self.state.send(tester.k1, c_addr, value_max - 3 * value)

        # number of tokens remaining is equal to 1*value (*creationRate)
        assert self.c.numberOfTokensLeft() == numTokensCreated
        assert self.c.totalSupply() == tokens_max - numTokensCreated

        # more than available tokens
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k2, c_addr, 2 * value)

        assert self.c.fundingActive() is True

        # exact amount of available tokens
        self.state.send(tester.k1, c_addr, value)
        assert self.c.numberOfTokensLeft() == 0
        assert self.c.totalSupply() == tokens_max

        # no tokens available
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k2, c_addr, value)

    # Check if the transfer() is locked during the funding period.
    def test_transfer_locked(self):
        addr, _ = self.deploy_contract(tester.a0, 1, 1)

        assert not self.c.fundingActive()

        self.state.mine(1)
        assert self.c.fundingActive()
        # Create tokens to meet minimum funding
        tokens = self.c.tokenCreationMin()
        value = tokens / self.c.tokenCreationRate()

        with self.event_listener(self.c, self.state) as listener:
            self.state.send(tester.k1, addr, value)
            assert listener.event('Transfer',
                                  _value=tokens,
                                  _to=tester.a1.encode('hex'),
                                  _from='0' * 40)
            assert not listener.events  # no more events

        assert self.balance_of(1) == tokens

        # At this point a1 has GNT but cannot transfer them.
        assert not self.c.finalized()

        with self.event_listener(self.c, self.state) as listener:
            with self.assertRaises(TransactionFailed):
                self.transfer(tester.k1, tester.a2, tokens)
            assert not listener.events

        # Funding has ended.
        self.state.mine(1)
        assert self.state.block.number == self.starting_block + 2
        assert self.c.fundingActive() is False

        self.c.finalize()
        assert self.c.finalized()

        with self.event_listener(self.c, self.state) as listener:
            assert self.transfer(tester.k1, tester.a2, tokens)
            assert listener.event('Transfer',
                                  _value=tokens,
                                  _to=tester.a2.encode('hex'),
                                  _from=tester.a1.encode('hex'))
            assert not listener.events  # no more events

        assert self.balance_of(1) == 0
        assert self.balance_of(2) == tokens

    def test_migration(self):
        s_addr, _ = self.deploy_contract(tester.a9, 2, 2)
        source = self.c

        tokens = source.tokenCreationCap()
        eths = tokens / source.tokenCreationRate()

        # pre funding
        self.state.mine(1)
        with self.assertRaises(ContractCreationFailed):
            self.deploy_migration_contract(s_addr)
        assert not source.finalized()

        # funding
        self.state.mine(1)
        self.state.send(tester.k1, s_addr, eths)

        with self.assertRaises(ContractCreationFailed):
            self.deploy_migration_contract(s_addr)
        assert not source.finalized()

        # post funding
        self.state.mine(1)

        with self.assertRaises(ContractCreationFailed):
            self.deploy_migration_contract(s_addr)

        assert not source.finalized()
        self._finalize_funding(s_addr, expected_supply=tokens)
        assert source.finalized()

        # migration and target token contracts
        m_addr, _ = self.deploy_migration_contract(s_addr)
        t_addr, _ = self.deploy_target_contract(m_addr)

        migration = self.m
        target = self.t

        source.setMigrationAgent(m_addr, sender=tester.k9)
        migration.setTargetToken(t_addr, sender=tester.k9)

        assert source.balanceOf(tester.a1) == tokens
        assert target.balanceOf(tester.a1) == 0

        with self.assertRaises(TransactionFailed):
            source.migrate(0, sender=tester.k1)

        with self.assertRaises(TransactionFailed):
            source.migrate(tokens + 1, sender=tester.k1)

        with self.event_listener(self.c, self.state) as listener:
            with self.assertRaises(TransactionFailed):
                source.migrate(tokens, sender=tester.k2)
            assert not listener.events

        # migrate tokens
        with self.event_listener(self.c, self.state) as listener:
            source.migrate(tokens, sender=tester.k1)
            assert listener.event('Transfer',
                                  _value=tokens,
                                  _from=m_addr.encode('hex'),
                                  _to=tester.a1.encode('hex'))
            assert listener.event('Migrate',
                                  _value=tokens,
                                  _from=tester.a1.encode('hex'),
                                  _to=m_addr.encode('hex'))
            assert not listener.events  # no more events

        with self.assertRaises(TransactionFailed):
            source.migrate(tokens, sender=tester.k1)

        assert source.balanceOf(tester.a1) == 0
        assert target.balanceOf(tester.a1) == tokens

        with self.assertRaises(TransactionFailed):
            source.migrate(tokens, sender=tester.k1)

        # finalize migration
        migration.finalizeMigration(sender=tester.k9)

        with self.assertRaises(TransactionFailed):
            migration.finalizeMigration(sender=tester.k9)

    def test_migration_master(self):
        s_addr, _ = self.deploy_contract(tester.a9, 2, 2, migration_master=tester.a8)
        source = self.c

        tokens = source.tokenCreationCap()
        eths = tokens / source.tokenCreationRate()

        self.state.mine(2)

        # funding
        self.state.send(tester.k1, s_addr, eths)

        # post funding
        self.state.mine(1)

        assert not source.finalized()
        self._finalize_funding(s_addr, expected_supply=tokens)
        assert source.finalized()

        # migration and target token contracts
        m_addr, _ = self.deploy_migration_contract(s_addr)
        t_addr, _ = self.deploy_target_contract(m_addr)

        migration = self.m
        target = self.t

        # attempt to enable migration using wrong keys
        with self.assertRaises(TransactionFailed):
            source.setMigrationAgent(m_addr, sender=tester.k9)
        with self.assertRaises(TransactionFailed):
            source.setMigrationAgent(m_addr, sender=tester.k7)

        with self.assertRaises(TransactionFailed):
            source.setMigrationMaster(tester.a7, sender=tester.k9)
        source.setMigrationMaster(tester.a7, sender=tester.k8)

        # attempt to enable migration using migration master keys
        source.setMigrationAgent(m_addr, sender=tester.k7)
        migration.setTargetToken(t_addr, sender=tester.k9)

        assert source.balanceOf(tester.a1) == tokens
        assert target.balanceOf(tester.a1) == 0

        with self.assertRaises(TransactionFailed):
            source.migrate(0, sender=tester.k1)

        with self.assertRaises(TransactionFailed):
            source.migrate(tokens + 1, sender=tester.k1)

    def test_multiple_migrations(self):
        s_addr, _ = self.deploy_contract(tester.a9, 1, 1)
        n_accounts = len(tester.accounts) - 1

        values = [0] * n_accounts

        # funding
        self.state.mine(1)

        # testers purchase tokens
        for i in range(0, n_accounts):
            value = random.randrange(150000 / 9, 150000 / 9 + 81) * denoms.ether
            self.state.send(tester.keys[i], s_addr, value)
            values[i] += value

        total = sum(values)
        source = self.c
        assert total * 1000 > source.tokenCreationMin()
        creation_rate = source.tokenCreationRate()

        assert source.totalSupply() == total * creation_rate

        # post funding
        self.state.mine(1)

        self._finalize_funding(s_addr, expected_supply=total * creation_rate)
        # additional tokens are generated for the golem agent and devs
        supply_after_finalization = source.totalSupply()

        m_addr, _ = self.deploy_migration_contract(s_addr)
        t_addr, _ = self.deploy_target_contract(m_addr)

        migration = self.m
        target = self.t

        source.setMigrationAgent(m_addr, sender=tester.k9)
        migration.setTargetToken(t_addr, sender=tester.k9)

        assert supply_after_finalization > total * creation_rate
        assert target.totalSupply() == 0

        # testers migrate tokens in parts
        for j in range(0, 10):
            for i in range(0, n_accounts):
                value = creation_rate * values[i] / 10
                source.migrate(value, sender=tester.keys[i])
                assert target.balanceOf(tester.accounts[i]) == value * (j + 1)

        migration.finalizeMigration(sender=tester.k9)

        assert source.totalSupply() == supply_after_finalization - total * creation_rate
        assert target.totalSupply() == total * creation_rate

    def test_number_of_tokens_left(self):
        addr, _ = self.deploy_contract(tester.a0, 13, 42)
        rate = self.c.tokenCreationRate()

        self.state.mine(13)
        tokens_max = self.c.numberOfTokensLeft()
        self.state.send(tester.k1, addr, tokens_max / rate / 2)
        assert self.c.numberOfTokensLeft() > 0

        self.state.mine(42 - 13 + 1)
        assert self.c.numberOfTokensLeft() == 0

    def test_send_raw_data_no_value(self):
        random_data = urandom(4)
        print("RANDOM DATA: {}".format(random_data.encode('hex')))
        addr, _ = self.deploy_contract(tester.a9, 7, 9)

        assert self.c.totalSupply() == 0
        assert self.contract_balance() == 0

        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k3, addr, value=0, evmdata=random_data)
        assert self.c.totalSupply() == 0
        assert self.contract_balance() == 0

        self.state.mine(7)
        assert self.c.fundingActive()
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k3, addr, value=0, evmdata=random_data)
        assert self.c.totalSupply() == 0
        assert self.contract_balance() == 0

        self.state.mine(3)
        assert not self.c.fundingActive()
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k3, addr, value=0, evmdata=random_data)
        assert self.c.totalSupply() == 0
        assert self.contract_balance() == 0

    def test_send_raw_data_and_value(self):
        random_data = urandom(4)
        print("RANDOM DATA: {}".format(random_data.encode('hex')))
        addr, _ = self.deploy_contract(tester.a9, 7, 9)

        max_value = self.c.tokenCreationCap() / self.c.tokenCreationRate()
        random_value = randint(0, max_value)
        print("RANDOM VALUE: {}".format(random_value))

        assert self.c.totalSupply() == 0
        assert self.contract_balance() == 0

        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k3, addr, random_value, evmdata=random_data)
        assert self.c.totalSupply() == 0
        assert self.contract_balance() == 0

        self.state.mine(7)
        assert self.c.fundingActive()
        self.state.send(tester.k3, addr, random_value, evmdata=random_data)
        assert self.c.totalSupply() == random_value * self.c.tokenCreationRate()
        assert self.contract_balance() == random_value

        self.state.mine(3)
        assert not self.c.fundingActive()
        with self.assertRaises(TransactionFailed):
            self.state.send(tester.k4, addr, random_value, evmdata=random_data)
        assert self.c.totalSupply() == random_value * self.c.tokenCreationRate()
        assert self.contract_balance() == random_value

    def test_send_value_through_other_function(self):
        addr, _ = self.deploy_contract(tester.a0, 17, 19)

        with self.assertRaises(TransactionFailed):
            self.c.totalSupply(value=13, sender=tester.k0)
        assert self.contract_balance() == 0

        with self.assertRaises(TransactionFailed):
            self.c.tokenCreationRate(value=13000000, sender=tester.k0)
        assert self.contract_balance() == 0

    def test_finalize_funding(self):
        self.deploy_contract(tester.accounts[9], 2, 2)
        contract = self.c
        allocation = tester.ABIContract(self.state, ALLOC_ABI, contract.lockedAllocation())

        # ---------------
        #   PRE FUNDING
        # ---------------
        self.state.mine(1)

        with self.assertRaises(TransactionFailed):
            contract.finalize()

        # ---------------
        #     FUNDING
        # ---------------
        self.state.mine(1)

        n_testers = len(tester.accounts) - 1
        eths = [(i + 1) * 10000 * denoms.ether for i in xrange(n_testers)]

        for i, e in enumerate(eths):
            self.state.send(tester.keys[i], contract.address, e)
            assert contract.balanceOf(tester.accounts[i]) == contract.tokenCreationRate() * e

        with self.assertRaises(TransactionFailed):
            contract.finalize()

        # ---------------
        #  POST FUNDING
        # ---------------
        self.state.mine(1)

        total_tokens = contract.totalSupply()
        assert total_tokens == sum(eths) * contract.tokenCreationRate()

        contract.finalize()
        with self.assertRaises(TransactionFailed):
            contract.finalize()

        factory_percent = 12
        devs_percent = 6
        sum_percent = factory_percent + devs_percent
        tokens_extra = total_tokens * sum_percent / (100 - sum_percent)

        assert contract.totalSupply() == total_tokens + tokens_extra
        assert contract.balanceOf(allocation.address) == tokens_extra

    def test_finalize_and_unlock(self):

        dev_shares = [2500, 730, 730, 730, 730, 730, 630, 630, 630, 630, 310,
                      153, 150, 100, 100, 100, 70, 70, 70, 70, 70, 42, 25]

        n_devs = len(dev_shares)
        contract, allocation, dev_keys, dev_accounts = self.deploy_contract_and_accounts(n_devs)
        factory = contract.golemFactory()

        # ---------------
        #     FUNDING
        # ---------------
        self.state.mine(2)

        n_testers = len(tester.accounts) - 1
        eths = [(i + 1) * 10000 * denoms.ether for i in xrange(n_testers)]

        for i, e in enumerate(eths):
            self.state.send(tester.keys[i], contract.address, e)

        # ---------------
        #  POST FUNDING
        # ---------------
        self.state.mine(1)

        total_tokens = contract.totalSupply()

        contract.finalize()

        ca_percent, devs_percent = 12, 6
        sum_percent = ca_percent + devs_percent

        tokens_extra = total_tokens * sum_percent / (100 - sum_percent)
        tokens_ca = tokens_extra * ca_percent / sum_percent
        tokens_devs = tokens_extra - tokens_ca

        # ---------------
        #   PRE UNLOCK
        # ---------------
        for i in xrange(n_devs):
            assert contract.balanceOf(dev_accounts[i]) == 0
        assert contract.balanceOf(factory) == 0

        with self.assertRaises(TransactionFailed):
            allocation.unlock(sender=tester.k9)

        # ---------------
        #    UNLOCKED
        # ---------------
        self.state.mine(1)
        self.state.block.timestamp += 1 * 10 ** 8

        balance_sum = 0

        allocation.unlock(sender=tester.k9)
        with self.assertRaises(TransactionFailed):
            allocation.unlock(sender=tester.k9)

        def assert_error_range(_value, _expected, n=1):
            magnitude = int(math.log10(_value)) if _value else 1
            err = _value / (10 ** (magnitude - n))
            assert _expected - err <= _value <= _expected + err

        for i in xrange(n_devs):
            allocation.unlock(sender=dev_keys[i])
            balance = contract.balanceOf(dev_accounts[i])
            expected = dev_shares[i] * tokens_devs / 10000

            assert_error_range(balance, expected)
            balance_sum += balance

        assert_error_range(tokens_devs, balance_sum)

        factory_balance = contract.balanceOf(factory)
        tokens_left = tokens_extra - factory_balance - balance_sum

        assert contract.balanceOf(factory) == tokens_ca
        assert contract.balanceOf(allocation.address) == tokens_left

    # assumes post funding period
    def _finalize_funding(self, addr, expected_supply):
        assert self.c.totalSupply() == expected_supply
        self.c.finalize()
        assert self.c.totalSupply() > expected_supply
        with self.assertRaises(TransactionFailed):
            self.c.finalize()

    def test_single_refund(self):
        addr, _ = self.deploy_contract(tester.a9, 0, 5)
        value = 150000 * denoms.ether - 1
        self.state.send(tester.k1, addr, value)
        assert self.c.totalSupply() == value * 1000
        self.state.mine(6)

        lg = self.state.block.gas_used
        b = self.state.block.get_balance(tester.a1)

        with self.event_listener(self.c, self.state) as listener:
            self.c.refund(sender=tester.k1)

            assert listener.event('Refund',
                                  _from=tester.a1.encode('hex'),
                                  _value=value)
            assert not listener.events  # no more events

        refund = self.state.block.get_balance(tester.a1) - b
        assert refund == value - (self.state.block.gas_used - lg) * tester.gas_price

        b = self.state.block.get_balance(tester.a2)
        with self.assertRaises(TransactionFailed):
            self.c.refund(sender=tester.k2)
        refund = self.state.block.get_balance(tester.a2) - b
        assert refund < 0

    def test_refund_disabled(self):
        addr, _ = self.deploy_contract(tester.a9, 0, 5)
        value = 150000 * denoms.ether - 1
        self.state.send(tester.k1, addr, value)
        assert self.c.totalSupply() == value * 1000
        self.state.mine(3)
        self.state.send(tester.k2, addr, 1)
        assert self.c.totalSupply() == 150000000 * denoms.ether
        self.state.mine(10)
        with self.assertRaises(TransactionFailed):
            self.c.refund(sender=tester.k1)
        with self.assertRaises(TransactionFailed):
            self.c.refund(sender=tester.k2)
        with self.assertRaises(TransactionFailed):
            self.c.refund(sender=tester.k3)
            self.c.finalize()

    def test_wallet_deploy(self):
        founder = tester.accounts[9]
        key = tester.keys[9]
        wallet_addr, _ = self.deploy_wallet(founder)
        c_addr = self.deploy_contract_on_wallet(wallet_addr, 1, 1)
        self.state.mine(1)

    def test_payment_to_wallet(self):
        founder = tester.accounts[9]
        key = tester.keys[9]
        wallet_addr, _ = self.deploy_wallet(founder)
        self.state.mine(1)
        value = 11000
        self.state.send(tester.keys[0], wallet_addr, value)
        initial_b = self.state.block.get_balance(wallet_addr)
        self.state.send(tester.keys[0], wallet_addr, value)
        self.state.mine(1)
        current_b = self.state.block.get_balance(wallet_addr)
        assert current_b == initial_b + value

    def test_wallet_looping_in_payable(self):
        founder = tester.accounts[9]
        key = tester.keys[9]
        wallet_addr, g0 = self.deploy_wallet(founder)
        assert 0 == self.wallet.get_out_i(sender=key)
        self.wallet.set_extra_work(1, sender=key)
        extra = 10 # 601 passes, 602 fails send
        value = 11000
        self.wallet.set_extra_work(extra)
        self.state.mine(1)
        initial_b = self.state.block.get_balance(wallet_addr)
        self.state.send(tester.keys[0], wallet_addr, value)
        self.state.mine(1)
        current_b = self.state.block.get_balance(wallet_addr)
        assert extra == self.wallet.get_out_i(sender=key)
        assert current_b == initial_b + value

    def test_good_wallet(self):
        founder = tester.accounts[9]
        key = tester.keys[9]
        wallet_addr, g0 = self.deploy_wallet(founder)
        c_addr = self.deploy_contract_on_wallet(wallet_addr, 1, 1)
        value = int(self.c.tokenCreationMin() / self.c.tokenCreationRate())
        self.state.mine(1)
        self.state.send(tester.k1, c_addr, value)
        self.state.mine(3)
        extra = 0
        self.wallet.set_extra_work(extra)
        assert extra == self.wallet.get_extra_work()
        initial_b = self.state.block.get_balance(wallet_addr)
        self.wallet.finalize(c_addr, sender=key)
        self.state.mine(1)
        current_b = self.state.block.get_balance(wallet_addr)
        assert current_b == initial_b + value
        assert extra == self.wallet.get_out_i(sender=key)

    def test_bad_wallet(self):
        founder = tester.accounts[9]
        key = tester.keys[9]
        wallet_addr, g0 = self.deploy_wallet(founder)
        c_addr = self.deploy_contract_on_wallet(wallet_addr, 1, 1)
        value = int(self.c.tokenCreationMin() / self.c.tokenCreationRate())
        self.state.mine(1)
        self.state.send(tester.k1, c_addr, value)
        self.state.mine(3)
        # extra>0 will cause Wallet fallback function to burn gas;
        # send executed from contract has hardcoded limit of gas (2300?)
        # which is so small that it's not enough to do anything except for receive ether
        extra = 1
        self.wallet.set_extra_work(extra)
        assert extra == self.wallet.get_extra_work()
        initial_cb = self.state.block.get_balance(c_addr)
        initial_wb = self.state.block.get_balance(wallet_addr)
        initial_fb = self.state.block.get_balance(founder)
        with self.assertRaises(TransactionFailed):
            self.wallet.finalize(c_addr, sender=key)
        self.state.mine(1)
        current_cb = self.state.block.get_balance(c_addr)
        current_wb = self.state.block.get_balance(wallet_addr)
        current_fb = self.state.block.get_balance(founder)
        assert 0 == self.wallet.get_out_i(sender=key) # OK
        assert current_cb == initial_cb
        assert current_wb == initial_wb
        assert current_fb < initial_fb
        # check if first failed attempt does not lock Token contract indefinitely
        extra = 0
        self.wallet.set_extra_work(extra)
        assert extra == self.wallet.get_extra_work()
        initial_wb = self.state.block.get_balance(wallet_addr)
        self.wallet.finalize(c_addr, sender=key)
        self.state.mine(1)
        current_wb = self.state.block.get_balance(wallet_addr)
        assert current_wb == initial_wb + value
        assert extra == self.wallet.get_out_i(sender=key)


class GNTContractHelperTest(unittest.TestCase):

    def test_sub(self):

        alloc_helper = ContractHelper(ALLOC_CONTRACT_PATH)
        # remove import
        alloc_helper.sub([''], regex=IMPORT_TOKEN_REGEX)

        assert alloc_helper.findall()[:6] == ['0xde00', '0xde01', '0xde02', '0xde03', '0xde04', '0xde05']
        alloc_helper.sub(['0xad00', '0xad01', '0xad02'])
        assert alloc_helper.findall()[:6] == ['0xad00', '0xad01', '0xad02', '0xde03', '0xde04', '0xde05']

        # replace import with contract source
        gnt_helper = ContractHelper(GNT_CONTRACT_PATH, regex=IMPORT_ALLOC_REGEX)
        gnt_helper.sub([alloc_helper.source])

        state = tester.state()
        contract = state.abi_contract(gnt_helper.source, language='solidity', sender=tester.k0)

        assert contract
