import logging
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Tuple

from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.db import transaction

from ..account.models import User
from ..core import analytics
from ..core.exceptions import AllocationError, InsufficientStock, InsufficientStockData
from ..core.tracing import traced_atomic_transaction
from ..core.transactions import transaction_with_commit_on_errors
from ..payment import (
    ChargeStatus,
    CustomPaymentChoices,
    PaymentError,
    TransactionKind,
    gateway,
)
from ..payment.models import Payment, Transaction
from ..payment.utils import create_payment
from ..warehouse.management import (
    deallocate_stock,
    deallocate_stock_for_order,
    decrease_stock,
    get_order_lines_with_track_inventory,
)
from ..warehouse.models import Stock
from . import (
    FulfillmentLineData,
    FulfillmentStatus,
    OrderLineData,
    OrderOrigin,
    OrderStatus,
    events,
    utils,
)
from .error_codes import OrderErrorCode
from .events import (
    draft_order_created_from_replace_event,
    fulfillment_refunded_event,
    fulfillment_replaced_event,
    order_replacement_created,
    order_returned_event,
)
from .models import Fulfillment, FulfillmentLine, Order, OrderLine
from .notifications import (
    send_fulfillment_confirmation_to_customer,
    send_order_canceled_confirmation,
    send_order_confirmed,
    send_order_refunded_confirmation,
    send_payment_confirmation,
)
from .utils import (
    order_line_needs_automatic_fulfillment,
    recalculate_order,
    restock_fulfillment_lines,
    update_order_status,
)

if TYPE_CHECKING:
    from ..app.models import App
    from ..plugins.manager import PluginsManager
    from ..warehouse.models import Warehouse

logger = logging.getLogger(__name__)


OrderLineIDType = int
QuantityType = int


def order_created(
    order: "Order",
    user: "User",
    app: Optional["App"],
    manager: "PluginsManager",
    from_draft: bool = False,
):
    events.order_created_event(order=order, user=user, app=app, from_draft=from_draft)
    manager.order_created(order)
    payment = order.get_last_payment()
    if payment:
        if order.is_captured():
            order_captured(
                order=order,
                user=user,
                app=app,
                amount=payment.total,
                payment=payment,
                manager=manager,
            )
        elif order.is_pre_authorized():
            order_authorized(
                order=order,
                user=user,
                app=app,
                amount=payment.total,
                payment=payment,
                manager=manager,
            )
    site_settings = Site.objects.get_current().settings
    if site_settings.automatically_confirm_all_new_orders:
        order_confirmed(order, user, app, manager)


def order_confirmed(
    order: "Order",
    user: "User",
    app: Optional["App"],
    manager: "PluginsManager",
    send_confirmation_email: bool = False,
):
    """Order confirmed.

    Trigger event, plugin hooks and optionally confirmation email.
    """
    events.order_confirmed_event(order=order, user=user, app=app)
    manager.order_confirmed(order)
    if send_confirmation_email:
        send_order_confirmed(order, user, app, manager)


def handle_fully_paid_order(
    manager: "PluginsManager",
    order: "Order",
    user: Optional["User"] = None,
    app: Optional["App"] = None,
):
    events.order_fully_paid_event(order=order, user=user, app=app)
    if order.get_customer_email():
        send_payment_confirmation(order, manager)

        if utils.order_needs_automatic_fulfillment(order):
            automatically_fulfill_digital_lines(order, manager)
    try:
        analytics.report_order(order.tracking_client_id, order)
    except Exception:
        # Analytics failing should not abort the checkout flow
        logger.exception("Recording order in analytics failed")
    manager.order_fully_paid(order)
    manager.order_updated(order)


@traced_atomic_transaction()
def cancel_order(
    order: "Order",
    user: Optional["User"],
    app: Optional["App"],
    manager: "PluginsManager",
):
    """Cancel order.

    Release allocation of unfulfilled order items.
    """

    events.order_canceled_event(order=order, user=user, app=app)

    deallocate_stock_for_order(order)
    order.status = OrderStatus.CANCELED
    order.save(update_fields=["status"])

    transaction.on_commit(lambda: manager.order_cancelled(order))
    transaction.on_commit(lambda: manager.order_updated(order))

    send_order_canceled_confirmation(order, user, app, manager)


def order_refunded(
    order: "Order",
    user: Optional["User"],
    app: Optional["App"],
    amount: "Decimal",
    payment: "Payment",
    manager: "PluginsManager",
):
    events.payment_refunded_event(
        order=order, user=user, app=app, amount=amount, payment=payment
    )
    manager.order_updated(order)

    send_order_refunded_confirmation(
        order, user, app, amount, payment.currency, manager
    )


def order_voided(
    order: "Order",
    user: Optional["User"],
    app: Optional["App"],
    payment: "Payment",
    manager: "PluginsManager",
):
    events.payment_voided_event(order=order, user=user, app=app, payment=payment)
    manager.order_updated(order)


def order_returned(
    order: "Order",
    user: Optional["User"],
    app: Optional["App"],
    returned_lines: List[Tuple[QuantityType, OrderLine]],
):
    order_returned_event(order=order, user=user, app=app, returned_lines=returned_lines)
    update_order_status(order)


@traced_atomic_transaction()
def order_fulfilled(
    fulfillments: List["Fulfillment"],
    user: "User",
    app: Optional["App"],
    fulfillment_lines: List["FulfillmentLine"],
    manager: "PluginsManager",
    notify_customer=True,
):
    order = fulfillments[0].order
    update_order_status(order)
    events.fulfillment_fulfilled_items_event(
        order=order, user=user, app=app, fulfillment_lines=fulfillment_lines
    )
    transaction.on_commit(lambda: manager.order_updated(order))

    for fulfillment in fulfillments:
        transaction.on_commit(lambda: manager.fulfillment_created(fulfillment))

    if order.status == OrderStatus.FULFILLED:
        transaction.on_commit(lambda: manager.order_fulfilled(order))

    if notify_customer:
        for fulfillment in fulfillments:
            send_fulfillment_confirmation_to_customer(
                order, fulfillment, user, app, manager
            )


@traced_atomic_transaction()
def order_awaits_fulfillment_approval(
    fulfillments: List["Fulfillment"],
    user: "User",
    app: Optional["App"],
    fulfillment_lines: List["FulfillmentLine"],
    manager: "PluginsManager",
    _notify_customer=True,
):
    order = fulfillments[0].order
    update_order_status(order)
    events.fulfillment_awaits_approval_event(
        order=order, user=user, app=app, fulfillment_lines=fulfillment_lines
    )
    transaction.on_commit(lambda: manager.order_updated(order))


def order_shipping_updated(order: "Order", manager: "PluginsManager"):
    recalculate_order(order)
    manager.order_updated(order)


def order_authorized(
    order: "Order",
    user: Optional["User"],
    app: Optional["App"],
    amount: "Decimal",
    payment: "Payment",
    manager: "PluginsManager",
):
    events.payment_authorized_event(
        order=order, user=user, app=app, amount=amount, payment=payment
    )
    manager.order_updated(order)


def order_captured(
    order: "Order",
    user: Optional["User"],
    app: Optional["App"],
    amount: "Decimal",
    payment: "Payment",
    manager: "PluginsManager",
):
    events.payment_captured_event(
        order=order, user=user, app=app, amount=amount, payment=payment
    )
    manager.order_updated(order)
    if order.is_fully_paid():
        handle_fully_paid_order(manager, order, user, app)


def fulfillment_tracking_updated(
    fulfillment: "Fulfillment",
    user: "User",
    app: Optional["App"],
    tracking_number: str,
    manager: "PluginsManager",
):
    events.fulfillment_tracking_updated_event(
        order=fulfillment.order,
        user=user,
        app=app,
        tracking_number=tracking_number,
        fulfillment=fulfillment,
    )
    manager.order_updated(fulfillment.order)


@traced_atomic_transaction()
def cancel_fulfillment(
    fulfillment: "Fulfillment",
    user: "User",
    app: Optional["App"],
    warehouse: Optional["Warehouse"],
    manager: "PluginsManager",
):
    """Cancel fulfillment.

    Return products to corresponding stocks if warehouse was defined.
    """
    fulfillment = Fulfillment.objects.select_for_update().get(pk=fulfillment.pk)
    events.fulfillment_canceled_event(
        order=fulfillment.order, user=user, app=app, fulfillment=fulfillment
    )
    if warehouse:
        restock_fulfillment_lines(fulfillment, warehouse)
        events.fulfillment_restocked_items_event(
            order=fulfillment.order,
            user=user,
            app=app,
            fulfillment=fulfillment,
            warehouse_pk=warehouse.pk,
        )
    fulfillment.status = FulfillmentStatus.CANCELED
    fulfillment.save(update_fields=["status"])
    update_order_status(fulfillment.order)
    transaction.on_commit(lambda: manager.fulfillment_canceled(fulfillment))
    transaction.on_commit(lambda: manager.order_updated(fulfillment.order))


@traced_atomic_transaction()
def approve_fulfillment(
    fulfillment: Fulfillment,
    user: "User",
    app: Optional["App"],
    manager: "PluginsManager",
    notify_customer=True,
):
    fulfillment.status = FulfillmentStatus.FULFILLED
    fulfillment.save()
    order = fulfillment.order
    if notify_customer:
        send_fulfillment_confirmation_to_customer(
            fulfillment.order, fulfillment, user, app, manager
        )
    events.fulfillment_fulfilled_items_event(
        order=order, user=user, app=app, fulfillment_lines=list(fulfillment.lines.all())
    )
    lines_to_fulfill = [
        OrderLineData(
            line=f_line.order_line,
            quantity=f_line.quantity,
            variant=f_line.order_line.variant,
            warehouse_pk=str(f_line.stock.warehouse_id),  # type: ignore
        )
        for f_line in fulfillment.lines.all()
    ]
    fulfill_order_lines(lines_to_fulfill)
    order.refresh_from_db()
    update_order_status(order)

    transaction.on_commit(lambda: manager.order_updated(order))
    if order.status == OrderStatus.FULFILLED:
        transaction.on_commit(lambda: manager.order_fulfilled(order))

    return fulfillment


@traced_atomic_transaction()
def mark_order_as_paid(
    order: "Order",
    request_user: "User",
    app: Optional["App"],
    manager: "PluginsManager",
    external_reference: Optional[str] = None,
):
    """Mark order as paid.

    Allows to create a payment for an order without actually performing any
    payment by the gateway.
    """

    payment = create_payment(
        gateway=CustomPaymentChoices.MANUAL,
        payment_token="",
        currency=order.total.gross.currency,
        email=order.user_email,
        total=order.total.gross.amount,
        order=order,
        external_reference=external_reference,
    )
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = order.total.gross.amount
    payment.save(update_fields=["captured_amount", "charge_status", "modified"])

    Transaction.objects.create(
        payment=payment,
        action_required=False,
        kind=TransactionKind.EXTERNAL,
        token=external_reference or "",
        is_success=True,
        amount=order.total.gross.amount,
        currency=order.total.gross.currency,
        gateway_response={},
    )
    events.order_manually_marked_as_paid_event(
        order=order,
        user=request_user,
        app=app,
        transaction_reference=external_reference,
    )
    manager.order_fully_paid(order)
    manager.order_updated(order)
    order.update_total_paid()


def clean_mark_order_as_paid(order: "Order"):
    """Check if an order can be marked as paid."""
    if order.payments.exists():
        raise PaymentError(
            "Orders with payments can not be manually marked as paid.",
        )


@traced_atomic_transaction()
def fulfill_order_lines(order_lines_info: Iterable["OrderLineData"]):
    """Fulfill order line with given quantity."""
    lines_to_decrease_stock = get_order_lines_with_track_inventory(order_lines_info)
    if lines_to_decrease_stock:
        decrease_stock(lines_to_decrease_stock)
    order_lines = []
    for line_info in order_lines_info:
        line = line_info.line
        line.quantity_fulfilled += line_info.quantity
        order_lines.append(line)

    OrderLine.objects.bulk_update(order_lines, ["quantity_fulfilled"])


@traced_atomic_transaction()
def automatically_fulfill_digital_lines(order: "Order", manager: "PluginsManager"):
    """Fulfill all digital lines which have enabled automatic fulfillment setting.

    Send confirmation email afterward.
    """
    digital_lines = order.lines.filter(
        is_shipping_required=False, variant__digital_content__isnull=False
    )
    digital_lines = digital_lines.prefetch_related("variant__digital_content")

    if not digital_lines:
        return
    fulfillment, _ = Fulfillment.objects.get_or_create(order=order)

    fulfillments = []
    lines_info = []
    for line in digital_lines:
        if not order_line_needs_automatic_fulfillment(line):
            continue
        variant = line.variant
        if variant:
            digital_content = variant.digital_content
            digital_content.urls.create(line=line)
        quantity = line.quantity
        fulfillments.append(
            FulfillmentLine(fulfillment=fulfillment, order_line=line, quantity=quantity)
        )
        warehouse_pk = line.allocations.first().stock.warehouse.pk  # type: ignore
        lines_info.append(
            OrderLineData(
                line=line,
                quantity=quantity,
                variant=line.variant,
                warehouse_pk=warehouse_pk,
            )
        )

    FulfillmentLine.objects.bulk_create(fulfillments)
    fulfill_order_lines(lines_info)

    send_fulfillment_confirmation_to_customer(
        order, fulfillment, user=order.user, app=None, manager=manager
    )
    update_order_status(order)


def _create_fulfillment_lines(
    fulfillment: Fulfillment,
    warehouse_pk: str,
    lines_data: List[Dict],
    channel_slug: str,
    decrease_stock: bool = True,
) -> List[FulfillmentLine]:
    """Modify stocks and allocations. Return list of unsaved FulfillmentLines.

    Args:
        fulfillment (Fulfillment): Fulfillment to create lines
        warehouse_pk (str): Warehouse to fulfill order.
        lines_data (List[Dict]): List with information from which system
            create FulfillmentLines. Example:
                [
                    {
                        "order_line": (OrderLine),
                        "quantity": (int),
                    },
                    ...
                ]
        channel_slug (str): Channel for which fulfillment lines should be created.
        decrease_stock (Bool): Stocks will get decreased if this is True.

    Return:
        List[FulfillmentLine]: Unsaved fulfillmet lines created for this fulfillment
            based on information form `lines`

    Raise:
        InsufficientStock: If system hasn't containt enough item in stock for any line.

    """
    lines = [line_data["order_line"] for line_data in lines_data]
    variants = [line.variant for line in lines]
    stocks = (
        Stock.objects.for_channel(channel_slug)
        .filter(warehouse_id=warehouse_pk, product_variant__in=variants)
        .select_related("product_variant")
    )

    variant_to_stock: Dict[str, List[Stock]] = defaultdict(list)
    for stock in stocks:
        variant_to_stock[stock.product_variant_id].append(stock)

    insufficient_stocks = []
    fulfillment_lines = []
    lines_info = []
    for line in lines_data:
        quantity = line["quantity"]
        order_line = line["order_line"]
        if quantity > 0:
            line_stocks = variant_to_stock.get(order_line.variant_id)
            if line_stocks is None:
                error_data = InsufficientStockData(
                    variant=order_line.variant,
                    order_line=order_line,
                    warehouse_pk=warehouse_pk,
                )
                insufficient_stocks.append(error_data)
                continue
            stock = line_stocks[0]
            lines_info.append(
                OrderLineData(
                    line=order_line,
                    quantity=quantity,
                    variant=order_line.variant,
                    warehouse_pk=warehouse_pk,
                )
            )
            if order_line.is_digital:
                order_line.variant.digital_content.urls.create(line=order_line)
            fulfillment_lines.append(
                FulfillmentLine(
                    order_line=order_line,
                    fulfillment=fulfillment,
                    quantity=quantity,
                    stock=stock,
                )
            )

    if insufficient_stocks:
        raise InsufficientStock(insufficient_stocks)

    if lines_info and decrease_stock:
        fulfill_order_lines(lines_info)

    return fulfillment_lines


@traced_atomic_transaction()
def create_fulfillments(
    user: "User",
    app: Optional["App"],
    order: "Order",
    fulfillment_lines_for_warehouses: Dict,
    manager: "PluginsManager",
    notify_customer: bool = True,
    approved: bool = True,
) -> List[Fulfillment]:
    """Fulfill order.

    Function create fulfillments with lines.
    Next updates Order based on created fulfillments.

    Args:
        user (User): User who trigger this action.
        app (App): App that trigger the action.
        order (Order): Order to fulfill
        fulfillment_lines_for_warehouses (Dict): Dict with information from which
            system create fulfillments. Example:
                {
                    (Warehouse.pk): [
                        {
                            "order_line": (OrderLine),
                            "quantity": (int),
                        },
                        ...
                    ]
                }
        manager (PluginsManager): Base manager for handling plugins logic.
        notify_customer (bool): If `True` system send email about
            fulfillments to customer.
        approved (Boolean): fulfillments will have status fulfilled if it's True,
            otherwise waiting_for_approval.

    Return:
        List[Fulfillment]: Fulfillmet with lines created for this order
            based on information form `fulfillment_lines_for_warehouses`


    Raise:
        InsufficientStock: If system hasn't containt enough item in stock for any line.

    """
    fulfillments: List[Fulfillment] = []
    fulfillment_lines: List[FulfillmentLine] = []
    status = (
        FulfillmentStatus.FULFILLED
        if approved
        else FulfillmentStatus.WAITING_FOR_APPROVAL
    )
    for warehouse_pk in fulfillment_lines_for_warehouses:
        fulfillment = Fulfillment.objects.create(order=order, status=status)
        fulfillments.append(fulfillment)
        fulfillment_lines.extend(
            _create_fulfillment_lines(
                fulfillment,
                warehouse_pk,
                fulfillment_lines_for_warehouses[warehouse_pk],
                order.channel.slug,
                decrease_stock=approved,
            )
        )

    FulfillmentLine.objects.bulk_create(fulfillment_lines)
    post_creation_func = (
        order_fulfilled if approved else order_awaits_fulfillment_approval
    )
    transaction.on_commit(
        lambda: post_creation_func(  # type: ignore
            fulfillments,
            user,
            app,
            fulfillment_lines,
            manager,
            notify_customer,
        )
    )
    return fulfillments


def _get_fulfillment_line_if_exists(
    fulfillment_lines: List[FulfillmentLine], order_line_id, stock_id=None
):
    for line in fulfillment_lines:
        if line.order_line_id == order_line_id and line.stock_id == stock_id:
            return line
    return None


def _get_fulfillment_line(
    target_fulfillment: Fulfillment,
    lines_in_target_fulfillment: List[FulfillmentLine],
    order_line_id: int,
    stock_id: Optional[int] = None,
) -> Tuple[FulfillmentLine, bool]:
    """Get fulfillment line if extists or create new fulfillment line object."""
    # Check if line for order_line_id and stock_id does not exist in DB.
    moved_line = _get_fulfillment_line_if_exists(
        lines_in_target_fulfillment,
        order_line_id,
        stock_id,
    )
    fulfillment_line_existed = True
    if not moved_line:
        # Create new not saved FulfillmentLine object and assign it to target
        # fulfillment
        fulfillment_line_existed = False
        moved_line = FulfillmentLine(
            fulfillment=target_fulfillment,
            order_line_id=order_line_id,
            stock_id=stock_id,
            quantity=0,
        )
    return moved_line, fulfillment_line_existed


@traced_atomic_transaction()
def _move_order_lines_to_target_fulfillment(
    order_lines_to_move: List[OrderLineData],
    target_fulfillment: Fulfillment,
) -> List[FulfillmentLine]:
    """Move order lines with given quantity to the target fulfillment."""
    fulfillment_lines_to_create: List[FulfillmentLine] = []
    order_lines_to_update: List[OrderLine] = []

    lines_to_dellocate: List[OrderLineData] = []
    for line_data in order_lines_to_move:
        line_to_move = line_data.line
        quantity_to_move = line_data.quantity

        # calculate the quantity fulfilled/unfulfilled to move
        unfulfilled_to_move = min(line_to_move.quantity_unfulfilled, quantity_to_move)
        line_to_move.quantity_fulfilled += unfulfilled_to_move

        fulfillment_line = FulfillmentLine(
            fulfillment=target_fulfillment,
            order_line_id=line_to_move.id,
            stock_id=None,
            quantity=unfulfilled_to_move,
        )

        # update current lines with new value of quantity
        order_lines_to_update.append(line_to_move)

        fulfillment_lines_to_create.append(fulfillment_line)

        line_allocations_exists = line_to_move.allocations.exists()
        if line_allocations_exists:
            lines_to_dellocate.append(
                OrderLineData(line=line_to_move, quantity=unfulfilled_to_move)
            )

    if lines_to_dellocate:
        try:
            deallocate_stock(lines_to_dellocate)
        except AllocationError as e:
            lines = [str(line.pk) for line in e.order_lines]
            logger.warning(
                "Unable to deallocate stock for lines.", extra={"lines": lines}
            )

    created_fulfillment_lines = FulfillmentLine.objects.bulk_create(
        fulfillment_lines_to_create
    )
    OrderLine.objects.bulk_update(order_lines_to_update, ["quantity_fulfilled"])
    return created_fulfillment_lines


@traced_atomic_transaction()
def _move_fulfillment_lines_to_target_fulfillment(
    fulfillment_lines_to_move: List[FulfillmentLineData],
    lines_in_target_fulfillment: List[FulfillmentLine],
    target_fulfillment: Fulfillment,
):
    """Move fulfillment lines with given quantity to the target fulfillment."""
    fulfillment_lines_to_create: List[FulfillmentLine] = []
    fulfillment_lines_to_update: List[FulfillmentLine] = []
    empty_fulfillment_lines_to_delete: List[FulfillmentLine] = []

    for fulfillment_line_data in fulfillment_lines_to_move:
        fulfillment_line = fulfillment_line_data.line
        quantity_to_move = fulfillment_line_data.quantity

        moved_line, fulfillment_line_existed = _get_fulfillment_line(
            target_fulfillment=target_fulfillment,
            lines_in_target_fulfillment=lines_in_target_fulfillment,
            order_line_id=fulfillment_line.order_line_id,
            stock_id=fulfillment_line.stock_id,
        )

        # calculate the quantity fulfilled/unfulfilled/to move
        fulfilled_to_move = min(fulfillment_line.quantity, quantity_to_move)
        quantity_to_move -= fulfilled_to_move
        moved_line.quantity += fulfilled_to_move
        fulfillment_line.quantity -= fulfilled_to_move

        if fulfillment_line.quantity == 0:
            # the fulfillment line without any items will be deleted
            empty_fulfillment_lines_to_delete.append(fulfillment_line)
        else:
            # update with new quantity value
            fulfillment_lines_to_update.append(fulfillment_line)

        if moved_line.quantity > 0 and not fulfillment_line_existed:
            # If this is new type of (order_line, stock) then we create new fulfillment
            # line
            fulfillment_lines_to_create.append(moved_line)
        elif fulfillment_line_existed:
            # if target fulfillment already have the same line, we  just update the
            # quantity
            fulfillment_lines_to_update.append(moved_line)

    # update the fulfillment lines with new values
    FulfillmentLine.objects.bulk_update(fulfillment_lines_to_update, ["quantity"])
    FulfillmentLine.objects.bulk_create(fulfillment_lines_to_create)

    # Remove the empty fulfillment lines
    FulfillmentLine.objects.filter(
        id__in=[f.id for f in empty_fulfillment_lines_to_delete]
    ).delete()


def __get_shipping_refund_amount(
    refund_shipping_costs: bool,
    refund_amount: Optional[Decimal],
    shipping_price: Decimal,
) -> Optional[Decimal]:
    # We set shipping refund amount only when refund amount is calculated by Saleor
    shipping_refund_amount = None
    if refund_shipping_costs and refund_amount is None:
        shipping_refund_amount = shipping_price
    return shipping_refund_amount


def create_refund_fulfillment(
    user: Optional["User"],
    app: Optional["App"],
    order,
    payment,
    order_lines_to_refund: List[OrderLineData],
    fulfillment_lines_to_refund: List[FulfillmentLineData],
    manager: "PluginsManager",
    amount=None,
    refund_shipping_costs=False,
):
    """Proceed with all steps required for refunding products.

    Calculate refunds for products based on the order's lines and fulfillment
    lines.  The logic takes the list of order lines, fulfillment lines, and their
    quantities which is used to create the refund fulfillment. The stock for
    unfulfilled lines will be deallocated.
    """

    shipping_refund_amount = __get_shipping_refund_amount(
        refund_shipping_costs, amount, order.shipping_price_gross_amount
    )

    with transaction_with_commit_on_errors():
        total_refund_amount = _process_refund(
            user=user,
            app=app,
            order=order,
            payment=payment,
            order_lines_to_refund=order_lines_to_refund,
            fulfillment_lines_to_refund=fulfillment_lines_to_refund,
            amount=amount,
            refund_shipping_costs=refund_shipping_costs,
            manager=manager,
        )

        refunded_fulfillment = Fulfillment.objects.create(
            status=FulfillmentStatus.REFUNDED,
            order=order,
            total_refund_amount=total_refund_amount,
            shipping_refund_amount=shipping_refund_amount,
        )
        created_fulfillment_lines = _move_order_lines_to_target_fulfillment(
            order_lines_to_move=order_lines_to_refund,
            target_fulfillment=refunded_fulfillment,
        )

        _move_fulfillment_lines_to_target_fulfillment(
            fulfillment_lines_to_move=fulfillment_lines_to_refund,
            lines_in_target_fulfillment=created_fulfillment_lines,
            target_fulfillment=refunded_fulfillment,
        )

        Fulfillment.objects.filter(
            order=order, lines=None, status=FulfillmentStatus.FULFILLED
        ).delete()
        transaction.on_commit(lambda: manager.order_updated(order))

    return refunded_fulfillment


def _populate_replace_order_fields(original_order: "Order"):
    replace_order = Order()
    replace_order.status = OrderStatus.DRAFT
    replace_order.user_id = original_order.user_id
    replace_order.language_code = original_order.language_code
    replace_order.user_email = original_order.user_email
    replace_order.currency = original_order.currency
    replace_order.channel = original_order.channel
    replace_order.display_gross_prices = original_order.display_gross_prices
    replace_order.redirect_url = original_order.redirect_url
    replace_order.original = original_order
    replace_order.origin = OrderOrigin.REISSUE
    replace_order.metadata = original_order.metadata
    replace_order.private_metadata = original_order.private_metadata

    if original_order.billing_address:
        original_order.billing_address.pk = None
        replace_order.billing_address = original_order.billing_address
        replace_order.billing_address.save()
    if original_order.shipping_address:
        original_order.shipping_address.pk = None
        replace_order.shipping_address = original_order.shipping_address
        replace_order.shipping_address.save()
    replace_order.save()
    original_order.refresh_from_db()
    return replace_order


@traced_atomic_transaction()
def create_replace_order(
    user: Optional[User],
    app: Optional["App"],
    original_order: "Order",
    order_lines_to_replace: List[OrderLineData],
    fulfillment_lines_to_replace: List[FulfillmentLineData],
) -> "Order":
    """Create draft order with lines to replace."""

    replace_order = _populate_replace_order_fields(original_order)
    order_line_to_create: Dict[OrderLineIDType, OrderLine] = dict()

    # iterate over lines without fulfillment to get the items for replace.
    # deepcopy to not lose the reference for lines assigned to original order
    for line_data in deepcopy(order_lines_to_replace):
        order_line = line_data.line
        order_line_id = order_line.pk
        order_line.pk = None
        order_line.order = replace_order
        order_line.quantity = line_data.quantity
        order_line.quantity_fulfilled = 0
        # we set order_line_id as a key to use it for iterating over fulfillment items
        order_line_to_create[order_line_id] = order_line

    order_lines_with_fulfillment = OrderLine.objects.in_bulk(
        [line_data.line.order_line_id for line_data in fulfillment_lines_to_replace]
    )
    for fulfillment_line_data in fulfillment_lines_to_replace:
        fulfillment_line = fulfillment_line_data.line
        order_line_id = fulfillment_line.order_line_id

        # if order_line_id exists in order_line_to_create, it means that we already have
        # prepared new order_line for this fulfillment. In that case we need to increase
        # quantity amount of new order_line by fulfillment_line.quantity
        if order_line_id in order_line_to_create:
            order_line_to_create[
                order_line_id
            ].quantity += fulfillment_line_data.quantity
            continue

        order_line_from_fulfillment = order_lines_with_fulfillment.get(order_line_id)
        order_line = order_line_from_fulfillment  # type: ignore
        order_line_id = order_line.pk
        order_line.pk = None
        order_line.order = replace_order
        order_line.quantity = fulfillment_line_data.quantity
        order_line.quantity_fulfilled = 0
        order_line_to_create[order_line_id] = order_line

    lines_to_create = order_line_to_create.values()
    OrderLine.objects.bulk_create(lines_to_create)

    recalculate_order(replace_order)

    draft_order_created_from_replace_event(
        draft_order=replace_order,
        original_order=original_order,
        user=user,
        app=app,
        lines=[(line.quantity, line) for line in lines_to_create],
    )
    return replace_order


def _move_lines_to_return_fulfillment(
    order_lines: List[OrderLineData],
    fulfillment_lines: List[FulfillmentLineData],
    fulfillment_status: str,
    order: "Order",
    total_refund_amount: Optional[Decimal],
    shipping_refund_amount: Optional[Decimal],
) -> Fulfillment:
    target_fulfillment = Fulfillment.objects.create(
        status=fulfillment_status,
        order=order,
        total_refund_amount=total_refund_amount,
        shipping_refund_amount=shipping_refund_amount,
    )
    lines_in_target_fulfillment = _move_order_lines_to_target_fulfillment(
        order_lines_to_move=order_lines,
        target_fulfillment=target_fulfillment,
    )

    fulfillment_lines_already_refunded = FulfillmentLine.objects.filter(
        fulfillment__order=order, fulfillment__status=FulfillmentStatus.REFUNDED
    ).values_list("id", flat=True)

    refunded_fulfillment_lines_to_return = []
    fulfillment_lines_to_return = []

    for line_data in fulfillment_lines:
        if line_data.line.id in fulfillment_lines_already_refunded:
            # item already refunded should be moved to fulfillment with status
            # REFUNDED_AND_RETURNED
            refunded_fulfillment_lines_to_return.append(line_data)
        else:
            # the rest of the items should be moved to target fulfillment
            fulfillment_lines_to_return.append(line_data)

    _move_fulfillment_lines_to_target_fulfillment(
        fulfillment_lines_to_move=fulfillment_lines_to_return,
        lines_in_target_fulfillment=lines_in_target_fulfillment,
        target_fulfillment=target_fulfillment,
    )

    if refunded_fulfillment_lines_to_return:
        if fulfillment_status == FulfillmentStatus.REFUNDED_AND_RETURNED:
            refund_and_return_fulfillment = target_fulfillment
        else:
            refund_and_return_fulfillment = Fulfillment.objects.create(
                status=FulfillmentStatus.REFUNDED_AND_RETURNED, order=order
            )
        _move_fulfillment_lines_to_target_fulfillment(
            fulfillment_lines_to_move=refunded_fulfillment_lines_to_return,
            lines_in_target_fulfillment=[],
            target_fulfillment=refund_and_return_fulfillment,
        )

    return target_fulfillment


def _move_lines_to_replace_fulfillment(
    order_lines_to_replace: List[OrderLineData],
    fulfillment_lines_to_replace: List[FulfillmentLineData],
    order: "Order",
) -> Fulfillment:
    target_fulfillment = Fulfillment.objects.create(
        status=FulfillmentStatus.REPLACED, order=order
    )
    lines_in_target_fulfillment = _move_order_lines_to_target_fulfillment(
        order_lines_to_move=order_lines_to_replace,
        target_fulfillment=target_fulfillment,
    )
    _move_fulfillment_lines_to_target_fulfillment(
        fulfillment_lines_to_move=fulfillment_lines_to_replace,
        lines_in_target_fulfillment=lines_in_target_fulfillment,
        target_fulfillment=target_fulfillment,
    )
    return target_fulfillment


@traced_atomic_transaction()
def create_return_fulfillment(
    user: Optional["User"],
    app: Optional["App"],
    order: "Order",
    order_lines: List[OrderLineData],
    fulfillment_lines: List[FulfillmentLineData],
    total_refund_amount: Optional[Decimal],
    shipping_refund_amount: Optional[Decimal],
) -> Fulfillment:
    status = FulfillmentStatus.RETURNED
    if total_refund_amount is not None:
        status = FulfillmentStatus.REFUNDED_AND_RETURNED
    with traced_atomic_transaction():
        return_fulfillment = _move_lines_to_return_fulfillment(
            order_lines=order_lines,
            fulfillment_lines=fulfillment_lines,
            fulfillment_status=status,
            order=order,
            total_refund_amount=total_refund_amount,
            shipping_refund_amount=shipping_refund_amount,
        )
        returned_lines: Dict[OrderLineIDType, Tuple[QuantityType, OrderLine]] = dict()
        order_lines_with_fulfillment = OrderLine.objects.in_bulk(
            [line_data.line.order_line_id for line_data in fulfillment_lines]
        )
        for line_data in order_lines:
            returned_lines[line_data.line.id] = (line_data.quantity, line_data.line)
        for line_data in fulfillment_lines:
            order_line = order_lines_with_fulfillment.get(line_data.line.order_line_id)
            returned_line = returned_lines.get(order_line.id)  # type: ignore
            if returned_line:
                quantity, line = returned_line
                quantity += line_data.quantity
                returned_lines[order_line.id] = (quantity, line)  # type: ignore
            else:
                returned_lines[order_line.id] = (  # type: ignore
                    line_data.quantity,
                    order_line,
                )
        returned_lines_list = list(returned_lines.values())
        transaction.on_commit(
            lambda: order_returned(
                order,
                user=user,
                app=app,
                returned_lines=returned_lines_list,
            )
        )

    return return_fulfillment


@traced_atomic_transaction()
def process_replace(
    user: Optional["User"],
    app: Optional["App"],
    order: "Order",
    order_lines: List[OrderLineData],
    fulfillment_lines: List[FulfillmentLineData],
) -> Tuple[Fulfillment, Optional["Order"]]:
    """Create replace fulfillment and new draft order.

    Move all requested lines to fulfillment with status replaced. Based on original
    order create the draft order with all user details, and requested lines.
    """

    replace_fulfillment = _move_lines_to_replace_fulfillment(
        order_lines_to_replace=order_lines,
        fulfillment_lines_to_replace=fulfillment_lines,
        order=order,
    )
    new_order = create_replace_order(
        user=user,
        app=app,
        original_order=order,
        order_lines_to_replace=order_lines,
        fulfillment_lines_to_replace=fulfillment_lines,
    )
    replaced_lines = [(line.quantity, line) for line in new_order.lines.all()]
    fulfillment_replaced_event(
        order=order,
        user=user,
        app=app,
        replaced_lines=replaced_lines,
    )
    order_replacement_created(
        original_order=order,
        replace_order=new_order,
        user=user,
        app=app,
    )

    return replace_fulfillment, new_order


def create_fulfillments_for_returned_products(
    user: Optional["User"],
    app: Optional["App"],
    order: "Order",
    payment: Optional[Payment],
    order_lines: List[OrderLineData],
    fulfillment_lines: List[FulfillmentLineData],
    manager: "PluginsManager",
    refund: bool = False,
    amount: Optional[Decimal] = None,
    refund_shipping_costs=False,
) -> Tuple[Fulfillment, Optional[Fulfillment], Optional[Order]]:
    """Process the request for replacing or returning the products.

    Process the refund when the refund is set to True. The amount of refund will be
    calculated for all lines with statuses different from refunded.  The lines which
    are set to replace will not be included in the refund amount.

    If the amount is provided, the refund will be used for this amount.

    If refund_shipping_costs is True, the calculated refund amount will include
    shipping costs.

    All lines with replace set to True will be used to create a new draft order, with
    the same order details as the original order.  These lines will be moved to
    fulfillment with status replaced. The events with relation to new order will be
    created.

    All lines with replace set to False will be moved to fulfillment with status
    returned/refunded_and_returned - depends on refund flag and current line status.
    If the fulfillment line has refunded status it will be moved to
    returned_and_refunded
    """
    return_order_lines = [data for data in order_lines if not data.replace]
    return_fulfillment_lines = [data for data in fulfillment_lines if not data.replace]

    shipping_refund_amount = __get_shipping_refund_amount(
        refund_shipping_costs, amount, order.shipping_price_gross_amount
    )
    total_refund_amount = None
    with traced_atomic_transaction():
        if refund and payment:
            total_refund_amount = _process_refund(
                user=user,
                app=app,
                order=order,
                payment=payment,
                order_lines_to_refund=return_order_lines,
                fulfillment_lines_to_refund=return_fulfillment_lines,
                amount=amount,
                refund_shipping_costs=refund_shipping_costs,
                manager=manager,
            )

        replace_order_lines = [data for data in order_lines if data.replace]
        replace_fulfillment_lines = [data for data in fulfillment_lines if data.replace]

        replace_fulfillment, new_order = None, None
        if replace_order_lines or replace_fulfillment_lines:
            replace_fulfillment, new_order = process_replace(
                user=user,
                app=app,
                order=order,
                order_lines=replace_order_lines,
                fulfillment_lines=replace_fulfillment_lines,
            )
        return_fulfillment = create_return_fulfillment(
            user=user,
            app=app,
            order=order,
            order_lines=return_order_lines,
            fulfillment_lines=return_fulfillment_lines,
            total_refund_amount=total_refund_amount,
            shipping_refund_amount=shipping_refund_amount,
        )
        Fulfillment.objects.filter(
            order=order, lines=None, status=FulfillmentStatus.FULFILLED
        ).delete()

        transaction.on_commit(lambda: manager.order_updated(order))
    return return_fulfillment, replace_fulfillment, new_order


def _calculate_refund_amount(
    return_order_lines: List[OrderLineData],
    return_fulfillment_lines: List[FulfillmentLineData],
    lines_to_refund: Dict[OrderLineIDType, Tuple[QuantityType, OrderLine]],
) -> Decimal:
    refund_amount = Decimal(0)
    for line_data in return_order_lines:
        refund_amount += line_data.quantity * line_data.line.unit_price_gross_amount
        lines_to_refund[line_data.line.id] = (line_data.quantity, line_data.line)

    if not return_fulfillment_lines:
        return refund_amount

    order_lines_with_fulfillment = OrderLine.objects.in_bulk(
        [line_data.line.order_line_id for line_data in return_fulfillment_lines]
    )
    for line_data in return_fulfillment_lines:
        # skip lines which were already refunded
        if line_data.line.fulfillment.status == FulfillmentStatus.REFUNDED:
            continue
        order_line = order_lines_with_fulfillment[line_data.line.order_line_id]
        refund_amount += line_data.quantity * order_line.unit_price_gross_amount

        data_from_all_refunded_lines = lines_to_refund.get(order_line.id)
        if data_from_all_refunded_lines:
            quantity, line = data_from_all_refunded_lines
            quantity += line_data.quantity
            lines_to_refund[order_line.id] = (quantity, line)
        else:
            lines_to_refund[order_line.id] = (line_data.quantity, order_line)
    return refund_amount


@transaction_with_commit_on_errors()
def _process_refund(
    user: Optional["User"],
    app: Optional["App"],
    order: "Order",
    payment: Payment,
    order_lines_to_refund: List[OrderLineData],
    fulfillment_lines_to_refund: List[FulfillmentLineData],
    amount: Optional[Decimal],
    refund_shipping_costs: bool,
    manager: "PluginsManager",
):
    lines_to_refund: Dict[OrderLineIDType, Tuple[QuantityType, OrderLine]] = dict()
    refund_amount = _calculate_refund_amount(
        order_lines_to_refund, fulfillment_lines_to_refund, lines_to_refund
    )
    if amount is None:
        amount = refund_amount
        # we take into consideration the shipping costs only when amount is not
        # provided.
        if refund_shipping_costs:
            amount += order.shipping_price_gross_amount
    if amount:
        amount = min(payment.captured_amount, amount)
        try:
            gateway.refund(
                payment, manager, amount=amount, channel_slug=order.channel.slug
            )
        except PaymentError:
            raise ValidationError(
                "The refund operation is not available yet.",
                code=OrderErrorCode.CANNOT_REFUND.value,
            )
        transaction.on_commit(
            lambda: events.payment_refunded_event(
                order=order,
                user=user,
                app=app,
                amount=amount,  # type: ignore
                payment=payment,
            )
        )
        transaction.on_commit(
            lambda: send_order_refunded_confirmation(
                order, user, app, amount, payment.currency, manager  # type: ignore
            )
        )

    transaction.on_commit(
        lambda: fulfillment_refunded_event(
            order=order,
            user=user,
            app=app,
            refunded_lines=list(lines_to_refund.values()),
            amount=amount,  # type: ignore
            shipping_costs_included=refund_shipping_costs,
        )
    )
    return amount
