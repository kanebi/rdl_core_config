import { patch } from "@web/core/utils/patch";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";

patch(ProductScreen.prototype, {
    get productsToDisplay() {
        const originalList = super.productsToDisplay;
        // Hide storable items if they have zero or negative stock
        return originalList.filter((product) => {
            const isStorable = product.is_storable || (product.raw && product.raw.is_storable);
            if (isStorable) {
                const qty = product.raw ? product.raw.pos_qty_available : product.pos_qty_available;
                return qty !== undefined && qty > 0;
            }
            return true; // Keep consumables and services
        });
    },

    async addProductToOrder(product) {
        if (this.pos.config.negative_stock_alert && (product.is_storable || product.raw.is_storable)) {
            const qty = product.raw.pos_qty_available;
            if (qty === undefined || qty <= 0) {
                this.dialog.add(ConfirmationDialog, {
                    title: _t("Negative Stock Warning"),
                    body: _t("This item '%s' has zero or negative stock (%s available). Do you want to add it to the order anyway?", product.display_name || product.name, qty || 0),
                    confirm: () => {
                        super.addProductToOrder(product);
                    },
                    cancel: () => {}
                });
                return;
            }
        }
        return super.addProductToOrder(product);
    }
});
