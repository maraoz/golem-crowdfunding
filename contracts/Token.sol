pragma solidity ^0.4.4;

import "./GNTAllocation.sol";
import "./FundedToken.sol";

contract MigrationAgent {
    function migrateFrom(address _from, uint256 _value);
}

contract GolemNetworkToken is FundedToken {
    string public constant name = "Golem Network Token";
    string public constant symbol = "GNT";
    uint8 public constant decimals = 18;  // 18 decimal places, the same as ETH.

    address public migrationAgent;
    uint256 public totalMigrated;

    event Migrate(address indexed _from, address indexed _to, uint256 _value);

    // Transfer GNT tokens from sender's account to provided account address.
    // This function is disabled during the funding.
    // Required state: Operational
    function transfer(address _to, uint256 _value) returns (bool) {
        // Abort if not in Operational state.
        if (fundingMode) throw;

        var senderBalance = balances[msg.sender];
        if (senderBalance >= _value && _value > 0) {
            senderBalance -= _value;
            balances[msg.sender] = senderBalance;
            balances[_to] += _value;
            Transfer(msg.sender, _to, _value);
            return true;
        }
        return false;
    }

    // Token migration support:

    function migrate(uint256 _value) external {
        // Abort if not in Operational Migration state.
        if (fundingMode) throw;
        if (migrationAgent == 0) throw;

        // Validate input value.
        if (_value == 0) throw;
        if (_value > balances[msg.sender]) throw;

        balances[msg.sender] -= _value;
        totalTokens -= _value;
        totalMigrated += _value;
        MigrationAgent(migrationAgent).migrateFrom(msg.sender, _value);
        Migrate(msg.sender, migrationAgent, _value);
    }

    // Set address of migration target contract and enable migration process.
    // Required state: Operational Normal
    // State transition: -> Operational Migration
    function setMigrationAgent(address _agent) external {
        // Abort if not in Operational Normal state.
        if (fundingMode) throw;
        if (migrationAgent != 0) throw;
        if (msg.sender != migrationMaster) throw;
        migrationAgent = _agent;
    }

    function setMigrationMaster(address _master) external {
        if (msg.sender != migrationMaster) throw;
        migrationMaster = _master;
    }
}
