# Nine Operators Replace Unlimited Event Types

Traditional event sourcing requires inventing domain-specific event types for every action. Across seven common domains, this produces roughly 200 ad-hoc event types — and every real system will have more. Each has its own schema, versioning needs, and handler code. None share semantics across domains.

EO replaces all of them with nine operators + domain-specific context.

---

## The Proliferation Problem

| Domain | Sample event types | Count |
|--------|-------------------|-------|
| E-Commerce | OrderCreated, OrderConfirmed, OrderApproved, OrderRejected, OrderCancelled, OrderShipped, OrderDelivered, OrderReturned, OrderRefunded, ItemAddedToCart, ShippingAddressUpdated, CouponApplied, TrackingNumberAssigned, DeliveryAttemptFailed... | ~28 |
| Payments | PaymentProcessed, PaymentFailed, PaymentRefunded, PaymentAuthorized, InvoiceGenerated, InvoicePaid, MoneyDeposited, TransferInitiated, AccountOpened, AccountClosed, FraudFlagged, SubscriptionCreated, SubscriptionCancelled... | ~30 |
| Customers | CustomerRegistered, CustomerVerified, CustomerDeactivated, EmailAddressChanged, PasswordChanged, ProfileUpdated, ConsentGiven, ConsentWithdrawn, LoyaltyPointsEarned, CustomerAnonymized... | ~18 |
| Inventory | InventoryReceived, InventoryAdjusted, InventoryReserved, StockDepleted, WarehouseTransferInitiated, ShipmentCreated, ShipmentDelivered, ShipmentLost, SupplierOrderPlaced, PackageScanned... | ~20 |
| Healthcare | PatientAdmitted, PatientDischarged, DiagnosisRecorded, TreatmentPrescribed, MedicationDispensed, LabTestOrdered, LabResultReceived, ReferralMade, ConsentObtained, ClaimSubmitted, AppointmentScheduled... | ~30 |
| HR | EmployeeHired, EmployeePromoted, EmployeeTerminated, SalaryAdjusted, LeaveRequested, LeaveApproved, PerformanceReviewCompleted, TrainingCompleted, TimesheetSubmitted, BenefitEnrolled... | ~22 |
| Content | DocumentCreated, DocumentEdited, DocumentPublished, DocumentArchived, DocumentDeleted, CommentAdded, ReviewRequested, ReviewApproved, TagAdded, VersionCreated... | ~18 |

**Total: ~200 ad-hoc event types across seven domains.** `OrderCancelled` and `AppointmentCancelled` and `SubscriptionCancelled` are structurally identical (something was nullified) but the system treats them as completely unrelated types.

---

## The EO Mapping

| Operator | Covers | What happened structurally |
|----------|--------|---------------------------|
| **INS** | Every Created, Recorded, Received, Registered, Admitted, Hired event | An observation was instantiated |
| **DES** | Every Assigned, Tagged, Flagged, Labeled, Categorized event | An external frame was applied |
| **REC** | Every Verified, Approved, Confirmed, Validated event | Recognition was granted |
| **SEG** | Every Transferred, Moved, Split, Separated event | A boundary was redrawn |
| **NUL** | Every Deleted, Destroyed, Terminated, Purged, Archived event | True destruction |
| **SYN** | Every Merged, Combined, Enrolled, Onboarded event | Components were fused |
| **ALT** | Every Updated, Changed, Adjusted, Revised, Transitioned event | Alternation between states |
| **SUP** | Every Disputed, Flagged for Review, Pending, Escalated event | Superposition of competing possibilities |
| **CON** | Every Linked, Associated, Referenced, Coupled event | Mutual constitution established |

**Nine operators. Universal semantics.** Every domain speaks the same language for absence, and every `WHERE NOT INS(x)` means the same thing whether you're in healthcare or e-commerce.

---

## What This Means in Practice

Instead of:
```
UserCreated, UserUpdated, UserEmailChanged, UserDeactivated,
OrderPlaced, OrderCanceled, PaymentProcessed, ...
```

You write:
```
INS(target: user, context: {domain: "auth", ...})
ALT(target: user.email, context: {domain: "auth", old: "...", new: "..."})
NUL(target: user.active, context: {domain: "auth", reason: "deactivation"})
```

Domain specificity lives in `context`, not in the event type. The operator tells you *what kind of structural transformation occurred*. The context tells you *in which domain, by whom, under what authority*. These are orthogonal concerns that traditional event sourcing conflates.
