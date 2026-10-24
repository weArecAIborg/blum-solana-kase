use anchor_lang::prelude::*;

#[error_code]
pub enum BondError {
    #[msg("Bond terms are invalid")]
    InvalidTerms,
    #[msg("Payment days do not match the regular schedule computed from the terms")]
    InvalidSchedule,
    #[msg("Arithmetic overflow")]
    Overflow,
    #[msg("The hook was called outside a token transfer")]
    NotTransferring,
    #[msg("Bonds may be held only in the owner's associated token account")]
    NotAssociatedTokenAccount,
    #[msg("Transfers are closed from the final record moment (redemption)")]
    TransfersClosedAtMaturity,
    #[msg("Placement is allowed only before the first record moment")]
    PlacementClosed,
    #[msg("Event index is outside the bond's schedule")]
    EventOutOfRange,
    #[msg("Instruction does not apply to this event type")]
    WrongEventKind,
    #[msg("The record moment has not been reached")]
    RecordMomentNotReached,
    #[msg("Payment for this event has not opened yet")]
    PaymentNotOpen,
    #[msg("The event is not funded")]
    NotFunded,
    #[msg("The event is already funded")]
    AlreadyFunded,
    #[msg("Vault balance does not cover the event's budget")]
    InsufficientFunding,
    #[msg("Already paid")]
    AlreadyPaid,
    #[msg("Nothing to pay")]
    NothingToPay,
    #[msg("Cash token has an extension that changes or conditions delivery (transfer fee, transfer hook, non-transferable), lets a third party move the vaults' cash (permanent delegate), or one this program cannot read")]
    UnsupportedCashToken,
    #[msg("Holder record and token account disagree: a balance changed outside the program")]
    PositionMismatch,
    #[msg("Offer terms are invalid (zero price or quantity, empty window, or closing after the final record moment)")]
    InvalidOffer,
    #[msg("The offer is not open at this time")]
    OfferNotOpen,
    #[msg("The offer window has not closed yet")]
    OfferStillOpen,
    #[msg("The offer is not funded")]
    OfferNotFunded,
    #[msg("Instruction does not apply to this offer type")]
    WrongOfferKind,
    #[msg("Quantity exceeds what the offer still accepts")]
    ExceedsOffer,
    #[msg("Quantity exceeds the holder's unlocked bonds")]
    InsufficientUnlocked,
    #[msg("Bonds are locked in a tender")]
    BondsLocked,
    #[msg("Tender already settled")]
    AlreadySettled,
    #[msg("Issue too large: a coupon or the redemption for this supply would not fit the cash token's amount")]
    IssueTooLarge,
    #[msg("The recipient's cash account requires a memo: pass the SPL Memo program")]
    MemoProgramMissing,
    #[msg("Placement would exceed the bond's issue size")]
    ExceedsIssueSize,
    #[msg("Tenders are resolved in the order they were made: resolve the earlier ones first")]
    TenderOutOfOrder,
    #[msg("Not a Solana DvP trade of this bond's tokens for its cash, between two registered holders, delivered to themselves")]
    InvalidTrade,
    #[msg("A trade escrow's record is not a holder: it is paid nothing, tenders nothing and redeems nothing")]
    EscrowRecord,
    #[msg("Bonds in a trade escrow may go only to the trade's buyer or back to its seller")]
    EscrowDestination,
}
