pragma solidity ^0.4.4;

contract FundedToken {
    uint256 public constant tokenCreationRate = 1000;

    // The funding cap in weis.
    uint256 public constant tokenCreationCap = 820000 ether * tokenCreationRate;
    uint256 public constant tokenCreationMin = 150000 ether * tokenCreationRate;

    uint256 fundingStartBlock;
    uint256 fundingEndBlock;

    // The flag indicates if the GNT contract is in "funding" mode.
    bool fundingMode = true;

    // Receives ETH and its own GNT endowment.
    address public golemFactory;

    // Has control over token migration to next version of token.
    address public migrationMaster;

    GNTAllocation public lockedAllocation;

    // The current total token supply.
    uint256 totalTokens;

    mapping (address => uint256) balances;

    event Transfer(address indexed _from, address indexed _to, uint256 _value);
    event Refund(address indexed _from, uint256 _value);

    function GolemNetworkToken(address _golemFactory,
                               address _migrationMaster,
                               uint256 _fundingStartBlock,
                               uint256 _fundingEndBlock) {
        lockedAllocation = new GNTAllocation(_golemFactory);
        migrationMaster = _migrationMaster;
        golemFactory = _golemFactory;
        fundingStartBlock = _fundingStartBlock;
        fundingEndBlock = _fundingEndBlock;
    }
    
    function totalSupply() external constant returns (uint256) {
        return totalTokens;
    }

    function balanceOf(address _owner) external constant returns (uint256) {
        return balances[_owner];
    }

    // Crowdfunding:

    function fundingActive() constant external returns (bool) {
        // Copy of inFundingActive.
        if (!fundingMode) return false;

        // b ≥ Start and b ≤ End and t < Max
        if (block.number < fundingStartBlock ||
            block.number > fundingEndBlock ||
            totalTokens >= tokenCreationCap) return false;
        return true;
    }

    // Helper function to get number of tokens left during the funding.
    function numberOfTokensLeft() constant external returns (uint256) {
        if (!fundingMode) return 0;
        if (block.number > fundingEndBlock) return 0;
        return tokenCreationCap - totalTokens;
    }

    function finalized() constant external returns (bool) {
        return !fundingMode;
    }

    // Create tokens when funding is active.
    // Required state: Funding Active
    // State transition: -> Funding Success (only if cap reached)
    function() payable external {
        // Abort if not in Funding Active state.
        // The checks are split (instead of using or operator) because it is
        // cheaper this way.
        if (!fundingMode) throw;
        if (block.number < fundingStartBlock) throw;
        if (block.number > fundingEndBlock) throw;
        if (totalTokens >= tokenCreationCap) throw;

        // Do not allow creating 0 tokens.
        if (msg.value == 0) throw;

        // Do not create more than cap
        var numTokens = msg.value * tokenCreationRate;
        totalTokens += numTokens;
        if (totalTokens > tokenCreationCap) throw;

        // Assign new tokens to the sender
        balances[msg.sender] += numTokens;

        // Log token creation event
        Transfer(0, msg.sender, numTokens);
    }

    // If cap was reached or crowdfunding has ended then:
    // transfer ETH to the Golem Factory address,
    // create GNT for the golemFactory (representing the company,
    // create GNT for the developers.
    // Required state: Funding Success
    // State transition: -> Operational Normal
    function finalize() external {
        // Abort if not in Funding Success state.
        if (!fundingMode) throw;
        if ((block.number <= fundingEndBlock ||
             totalTokens < tokenCreationMin) &&
            totalTokens < tokenCreationCap) throw;

        // Switch to Operational state. This is the only place this can happen.
        fundingMode = false;

        // Transfer ETH to the Golem Factory address.
        if (!golemFactory.send(this.balance)) throw;

        // Create additional GNT for the Factory (representing the company)
        // and developers as a 18% of total number of tokens.
        // All additional tokens are transfered to the account controller by
        // GNTAllocation contract which will not allow using them for 6 months.
        uint256 percentOfTotal = 18;
        uint256 additionalTokens =
            totalTokens * percentOfTotal / (100 - percentOfTotal);
        totalTokens += additionalTokens;
        balances[lockedAllocation] += additionalTokens;
        Transfer(0, lockedAllocation, additionalTokens);
    }

    // Get back the ether sent during the funding in case the funding has not
    // reached the minimum level.
    // Required state: Funding Failure
    function refund() external {
        // Abort if not in Funding Failure state.
        if (!fundingMode) throw;
        if (block.number <= fundingEndBlock) throw;
        if (totalTokens >= tokenCreationMin) throw;

        var gntValue = balances[msg.sender];
        if (gntValue == 0) throw;
        balances[msg.sender] = 0;
        totalTokens -= gntValue;

        var ethValue = gntValue / tokenCreationRate;
        if (!msg.sender.send(ethValue)) throw;
        Refund(msg.sender, ethValue);
    }
}
