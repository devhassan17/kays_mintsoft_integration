# Mintsoft REST API V1 Specification & Odoo 19 Mapping

## 1. Verified Environment & Authentication

- **Base URL**: `https://api.mintsoft.co.uk/api`
- **Swagger Spec**: `https://api.mintsoft.co.uk/swagger/docs/V1`
- **Authentication Header**: `ms-apikey: <API_KEY>`
- **TLS / HTTPS**: Mandatory HTTPS with strict certificate verification (`verify=True`) and host allowlisting (`api.mintsoft.co.uk` by default).

### Verified Endpoint Permissions (Moyee Coffee UK Environment)

| Endpoint | HTTP Method | Verified Result | Notes |
|---|---|---|---|
| `/api/Warehouse` | `GET` | `HTTP 200` (1 record) | Returns Warehouse ID `3` (`Main Warehouse`) |
| `/api/Product/List` | `GET` | `HTTP 200` (34 records) | Paged product list (`PageNo`, `Limit` max 100) |
| `/api/Order/List` | `GET` | `HTTP 200` (100 records) | Paged order list (`PageNo`, `Limit` max 100) |
| `/api/Product/StockLevels` | `GET` | `HTTP 200` (34 records) | Returns `StockLevel` array (`Level`, `TotalStockLevel`, `WarehouseId`, `ProductId`, `SKU`) |
| `/api/Client` | `GET` | `HTTP 401` | Restricted to Mintsoft Admin users only (expected for client-scoped API keys) |
| `/api/Client/SubClients` | `GET` | `HTTP 401` | Restricted to Mintsoft MasterClient users only (expected for single-client keys) |

> **Important Rule**: A `401` response on `/api/Client` or `/api/Client/SubClients` indicates that the API key is a standard Client-level key rather than a 3PL Admin key. It must never be treated as an authentication failure when Warehouse, Product, Order, and Stock endpoints succeed.

---

## 2. Confirmed Endpoints Used by the Module

### 2.1 Reference Data, Warehouses & Couriers
- `GET /api/Warehouse` -> Returns `Array<Warehouse>` (`ID`, `Name`, `Code`, `Active`, `AddressLine1`, `City`, `PostCode`, `CountryId`, `ClientId`).
- `GET /api/Courier/Services` -> Returns `Array<CourierService>` (`ID`, `Name`, `CourierServiceTypeId`, `TrackingURL`, `ActiveB`).
- `GET /api/Courier/ServiceTypes` -> Returns `Array<CourierServiceType>` (`ID`, `Name`, `Active`).
- `GET /api/Courier/Services/{id}/ExternalNames` -> Returns `Array<ExternalCourierServiceName>`.
- `GET /api/Order/Channels` -> Returns `Array<Channel>` (`ID`, `Name`, `Description`, `Active`, `ClientId`).
- `GET /api/Order/Statuses` -> Returns `Array<OrderStatus>` (`ID`, `Name`, `ExternalName`).
- `GET /api/RefData/Countries` -> Returns `Array<Country>` (`ID`, `Name`, `Code`, `Code3`).
- `GET /api/RefData/Currencies` -> Returns `Array<Currency>` (`ID`, `Name`, `Code`, `Symbol`).

### 2.2 Orders & Fulfilment
- `PUT /api/Order` -> Creates a new order. Body is `NewOrderWithItems`. Returns `Array<NewOrderResult>`:
  ```json
  [
    {
      "OrderId": 123456,
      "DropShipOrderId": 0,
      "OrderNumber": "S10216",
      "Success": true,
      "OrderStatusId": 1,
      "OrderStatus": "New",
      "Message": "Order Created",
      "OrderItems": [{"SKU": "COFFEE-01", "ID": 987654}]
    }
  ]
  ```
- `GET /api/Order/GetOrderId?orderNumber={orderNumber}&warehouseId={warehouseId}&clientId={clientId}` -> Exact match lookup by `orderNumber`.
- `GET /api/Order/Search?OrderNumber={term}&exactMatch=true&includeOrderItems=true` -> Searches orders by `OrderNumber`, `TrackingNumber`, or `ExternalOrderReference` (up to 10 most recent).
- `GET /api/Order/List` -> Paged order listing (`OrderStatusId`, `ClientId`, `WarehouseId`, `PageNo`, `Limit`, `SinceLastUpdated`, `SinceDespatchDate`, `IncludeOrderItems`).
- `GET /api/Order/{id}` -> Full `Order` details by Mintsoft Order `ID`.
- `POST /api/Order/{id}` -> Updates header details (`NewOrder`) on an existing order in an eligible state.
- `GET /api/Order/{id}/Items` -> Lists `Array<OrderItem>` on an order.
- `PUT /api/Order/{id}/Items` -> Adds a `NewOrderItem` to an existing order.
- `POST /api/Order/{id}/Items/{ItemId}` -> Updates an existing `OrderItem`.
- `DELETE /api/Order/{id}/Items/{ItemId}` -> Deletes an `OrderItem`.
- `GET /api/Order/{id}/Splits` -> Lists `Array<OrderSplit>` (`OrderId`, `SplitOrderId`, `SplitOrder`).
- `GET /api/Order/{id}/Shipments` -> Lists `Array<OrderShipment>` (`ID`, `OrderId`, `CourierType`, `Number`, `ShipmentDate`, `LabelURL`, `ParcelNumbers`, `Manifested`).
- `GET /api/Order/Shipments/TrackingEvents/List` -> Lists `Array<OrderShipmentTrackingEvent>` (`PageNo`, `Limit`, `TrackingStatusId`, `SinceLastUpdated`).
- `GET /api/Order/{id}/ConnectActions` & `PUT /api/Order/{id}/ConnectActions` -> Retrieves or registers per-order `NewOrderConnectAction` callback records.

### 2.3 Mutating GET Endpoints (Critical Safety Warning)
In Mintsoft's API design, several `GET` endpoints perform state mutations! Our client explicitly classifies these as **WRITE (mutating)** operations so they are **blocked in Dry Run mode** and never invoked during read-only polling:
- `GET /api/Order/{id}/Cancel` (Cancels an order — returns `ToolkitResult`)
- `GET /api/Order/{id}/CancelNoStock`
- `GET /api/Order/{id}/MarkAwaitingConfirmation`
- `GET /api/Order/{id}/MarkAwaitingPayment`
- `GET /api/Order/{id}/MarkConfirmed`
- `GET /api/Order/{id}/MarkDespatched`
- `GET /api/Order/{id}/MarkPackAndHold`
- `GET /api/Order/{id}/MarkPaymentReceived`
- `GET /api/Order/{id}/MarkPrinted`
- `GET /api/Order/{id}/MarkReleasePackAndHold`
- `GET /api/Order/{id}/UpdateNumberOfParcels`

### 2.4 Products & Inventory
- `GET /api/Product/List` -> Paged product listing (`PageNo`, `Limit`, `ClientId`, `SinceLastUpdated`).
- `GET /api/Product/Search?Search={term}&IncludeBundles=true&IncludeDiscontinued=false` -> Searches products by SKU/Name.
- `GET /api/Product/LookupProductId?SKU={sku}&ClientId={clientId}` -> Looks up integer `ProductId` by SKU or Alt Code.
- `GET /api/Product/{id}` -> Full `Product` record.
- `GET /api/Product/{id}/Bundle` -> Full `Bundle` record (`SKU`, `Name`, `Components` array of `BundleItem`).
- `PUT /api/Product` -> Creates a new product (`NewProductResult`).
- `POST /api/Product` -> Updates an existing product (`ToolkitResult`).
- `GET /api/Product/StockLevels` -> Returns `Array<StockLevel>` (`ProductId`, `WarehouseId`, `ClientId`, `SKU`, `Level`, `TotalStockLevel`, `PreOrderable`, `Bundle`, `LowStockLevel`, `LastUpdated`, `Breakdown`).
- `GET /api/Product/StockLevels/UpdatedSince?FromDate={isoDate}` -> Returns `Array<int>` of `ProductId`s whose stock changed since `FromDate`.
- `GET /api/Product/StockLevelsByWarehouse?WarehouseId={id}&LastUpdatedSince={isoDate}` -> Returns raw `Array<StockLevel>` updated since `LastUpdatedSince`.
- `GET /api/Product/Inventory/Bulk` -> Paged `Array<BulkInventoryItem>` (`ProductId`, `SKU`, `WarehouseId`, `StockLevel`, `Allocated`, `OnHand`, `OffHand`, `InQuarantine`, `InTransit`, `Scrapped`).

### 2.5 Returns (RMA)
- `POST /api/Return/CreateReturn/{OrderId}?WarehouseId={warehouseId}&Reference={ref}` -> Creates a return linked to a Mintsoft Order (`ToolkitResult`, where `ID` is the new `ReturnId`).
- `POST /api/Return/{id}/AddItem` -> Adds a `ReturnItemAPI` (`ProductId`, `Quantity`, `ReturnReasonId`, `Action`, `Comments`) to Return `{id}`.
- `POST /api/Return/CreateExternalReturn` -> Creates an unlinked return (`CreateExternalReturnRequest`).
- `GET /api/Return/List` -> Paged list of `Return` records (`ClientId`, `WarehouseId`, `PageNo`, `Limit`, `SinceLastUpdated`, `Confirmed`, `IncludeReturnItems`).
- `GET /api/Return/{id}` & `GET /api/Return/{id}/Items` -> Retrieves a single return and its items (`ReturnItem`).
- `GET /api/Return/Reasons` -> Lists valid `ReturnReason` records (`ID`, `Name`, `StockAction`, `Active`).

---

## 3. Field Mapping — Odoo `sale.order` to Mintsoft `NewOrderWithItems` (`PUT /api/Order`)

| Mintsoft Field | Type | Odoo Source | Notes |
|---|---|---|---|
| `OrderNumber` | `string` | Computed External Reference (`S10216` or `MCUK-S10216`) | Deterministic, unique per order |
| `ExternalOrderReference` | `string` | `sale.order.client_order_ref` or `OrderNumber` | Stable external reference |
| `ClientId` | `int32` | `instance.mintsoft_client_id` | Included when configured (> 0) |
| `WarehouseId` | `int32` | Mapped `mintsoft_warehouse_id` (e.g. `3`) | Required warehouse target |
| `FirstName` | `string` | Parsed from `partner_shipping_id.name` | Required by Mintsoft |
| `LastName` | `string` | Parsed surname from `partner_shipping_id.name` (or `-`) | |
| `CompanyName` | `string` | `partner_shipping_id.commercial_company_name` | |
| `Address1` | `string` | `partner_shipping_id.street` | Required by Mintsoft |
| `Address2` | `string` | `partner_shipping_id.street2` | |
| `Town` | `string` | `partner_shipping_id.city` | |
| `County` | `string` | `partner_shipping_id.state_id.name` | |
| `PostCode` | `string` | `partner_shipping_id.zip` | Required by Mintsoft |
| `Country` | `string` | `partner_shipping_id.country_id.code` | ISO-2 country code (e.g. `GB`) |
| `CountryId` | `int32` | Mapped Mintsoft `CountryId` (if fetched) | Optional when `Country` code passed |
| `Email` | `string` | `partner_shipping_id.email` or `partner_id.email` | |
| `Phone` | `string` | `partner_shipping_id.phone` or `partner_id.phone` | |
| `CourierServiceId` | `int32` | `carrier_mapping.mintsoft_service_id` | Mapped from `sale.order.carrier_id` |
| `CourierService` | `string` | `carrier_mapping.mintsoft_service_name` | |
| `ChannelId` | `int32` | `instance.mintsoft_channel_id` (if set) | |
| `Currency` | `string` | `sale.order.currency_id.name` (e.g. `GBP`) | |
| `OrderValue` | `double` | `sale.order.amount_total` | Gross order total |
| `TotalVat` | `double` | `sale.order.amount_tax` | Total tax |
| `ShippingTotalExVat` | `double` | Sum of delivery lines `price_subtotal` | Delivery charges header field |
| `ShippingTotalVat` | `double` | Sum of delivery lines `price_tax` | Delivery tax header field |
| `DeliveryNotes` | `string` | Delivery note / shipping instructions | |
| `Tags` | `string` | Comma-separated tags (e.g. `ODOO,TEST` in test mode) | |
| `ConnectAction` | `NewOrderConnectAction` | Webhook callback metadata (when enabled) | |
| `OrderItems` | `Array<NewOrderItem>` | Physical/storable `sale.order.line` records | Excludes delivery & service lines |

### `NewOrderItem` Mapping

| Mintsoft Field | Type | Odoo Source | Notes |
|---|---|---|---|
| `SKU` | `string` | `product_mapping.mintsoft_sku` or `product_id.default_code` | Validated non-empty SKU |
| `ProductId` | `int32` | `product_mapping.mintsoft_product_id` (when known) | Optional if `SKU` provided |
| `Quantity` | `int32` | Converted integer quantity in Mintsoft UoM | Must be a positive integer |
| `UnitPrice` | `double` | Unit price ex-VAT (`price_subtotal / qty`) | Adjusted for UoM factor |
| `UnitPriceVat` | `double` | Unit VAT (`price_tax / qty`) | Adjusted for UoM factor |
| `Discount` | `double` | Line discount amount | |
| `Details` | `string` | `sale.order.line.name` | Truncated cleanly if needed |
