from __future__ import annotations

import unittest

from src.services.supplier_identity import supplier_identity_errors, supplier_source_text


SUPPLIER_NAME = 'ТОВ "Синтетичний Постач"'
BUYER_NAME = 'ТОВ "Тестовий Замов"'
SUPPLIER_CODE = "12345678"
BUYER_CODE = "87654321"
SUPPLIER_IBAN = "UA000000000000000000000000001"
BUYER_IBAN = "UA000000000000000000000000002"


def source(supplier_label: str = "Постачальник", buyer_label: str = "Одержувач послуг") -> str:
    return (
        f"{supplier_label}: {SUPPLIER_NAME}\nЄДРПОУ: {SUPPLIER_CODE}\nIBAN: {SUPPLIER_IBAN}\n"
        f"{buyer_label}: {BUYER_NAME}\nЄДРПОУ: {BUYER_CODE}\nIBAN: {BUYER_IBAN}\n"
        "Сума 12345678.99\nРахунок №87654321\n"
    )


class SupplierIdentityTest(unittest.TestCase):
    def check(self, text: str | None, name: str = SUPPLIER_NAME, code: str = SUPPLIER_CODE, iban: str = SUPPLIER_IBAN) -> list[str]:
        return supplier_identity_errors(name, code, iban, text)

    def test_accepts_one_coherent_explicit_supplier_tuple(self) -> None:
        self.assertEqual(self.check(source()), [])

    def test_rejects_customer_name_with_supplier_code_and_iban(self) -> None:
        self.assertEqual(self.check(source(), name=BUYER_NAME), ["supplier_name_mismatch_source"])

    def test_rejects_supplier_name_with_customer_code(self) -> None:
        self.assertEqual(self.check(source(), code=BUYER_CODE), ["supplier_tax_id_mismatch_source"])

    def test_rejects_supplier_name_with_customer_iban(self) -> None:
        self.assertEqual(self.check(source(), iban=BUYER_IBAN), ["supplier_iban_mismatch_source"])

    def test_rejects_complete_customer_tuple(self) -> None:
        self.assertEqual(len(self.check(source(), BUYER_NAME, BUYER_CODE, BUYER_IBAN)), 3)

    def test_missing_or_unlabelled_source_fails_closed(self) -> None:
        self.assertEqual(self.check(None), ["supplier_source_evidence_missing"])
        self.assertEqual(self.check(f"{SUPPLIER_NAME}\n{SUPPLIER_CODE}\n{SUPPLIER_IBAN}"), ["supplier_role_evidence_missing"])

    def test_multiple_supplier_blocks_are_ambiguous(self) -> None:
        text = source() + f"Виконавець: {BUYER_NAME}\n{BUYER_CODE}\n{BUYER_IBAN}"
        self.assertEqual(self.check(text), ["supplier_role_evidence_ambiguous"])
        self.assertIsNone(supplier_source_text(text))

    def test_all_parties_on_same_line_still_remain_separate(self) -> None:
        text = source().replace("\n", " | ")
        self.assertEqual(self.check(text), [])
        self.assertIn("supplier_name_mismatch_source", self.check(text, name=BUYER_NAME))

    def test_legal_form_full_and_abbreviated_are_equivalent(self) -> None:
        text = source().replace(SUPPLIER_NAME, 'Товариство з обмеженою відповідальністю\n«Синтетичний Постач»')
        self.assertEqual(self.check(text), [])

    def test_different_name_or_legal_form_is_not_fuzzy_matched(self) -> None:
        self.assertIn("supplier_name_mismatch_source", self.check(source(), name='ТОВ "Синтетичний"'))
        self.assertIn("supplier_name_mismatch_source", self.check(source(), name='АТ "Синтетичний Постач"'))

    def test_supplier_and_buyer_can_be_same_entity_if_tuple_matches_supplier(self) -> None:
        text = source().replace(BUYER_NAME, SUPPLIER_NAME).replace(BUYER_CODE, SUPPLIER_CODE).replace(BUYER_IBAN, SUPPLIER_IBAN)
        self.assertEqual(self.check(text), [])

    def test_unrelated_decimal_or_invoice_number_is_not_tax_id_evidence(self) -> None:
        text = source().replace(f"ЄДРПОУ: {SUPPLIER_CODE}", f"Сума {SUPPLIER_CODE}.99\nРахунок №{SUPPLIER_CODE}\nШтрихкод {SUPPLIER_CODE}")
        self.assertIn("supplier_tax_id_mismatch_source", self.check(text))

    def test_unlabelled_standalone_supplier_code_is_supported(self) -> None:
        self.assertEqual(self.check(source().replace(f"ЄДРПОУ: {SUPPLIER_CODE}", SUPPLIER_CODE)), [])

    def test_bare_recipient_is_not_inferred_as_payee(self) -> None:
        for label in ("Одержувач", "Отримувач", "Получатель", "Recipient"):
            with self.subTest(label=label):
                self.assertEqual(self.check(source(supplier_label=label)), ["supplier_role_evidence_missing"])

    def test_explicit_supplier_labels_in_supported_languages(self) -> None:
        for label in ("Постачальник", "Продавець", "Виконавець", "Поставщик", "Продавец", "Исполнитель", "Supplier", "Seller", "Service provider", "Одержувач коштів", "Отримувач платежу", "Payment recipient"):
            with self.subTest(label=label):
                self.assertEqual(self.check(source(supplier_label=label)), [])

    def test_all_buyer_roles_delimit_supplier_evidence(self) -> None:
        for label in ("Покупець", "Замовник", "Платник", "Одержувач", "Отримувач", "Одержувач товарів", "Получатель услуг", "Клієнт", "Клиент", "Споживач", "Потребитель", "Вантажоодержувач", "Buyer", "Payer", "Service recipient", "Client", "Consumer", "Consignee"):
            with self.subTest(label=label):
                block = supplier_source_text(source(buyer_label=label)) or ""
                self.assertNotIn(BUYER_NAME, block)
                self.assertNotIn(BUYER_CODE, block)
                self.assertNotIn(BUYER_IBAN, block)
                self.assertIn("supplier_tax_id_mismatch_source", self.check(source(buyer_label=label), code=BUYER_CODE))

    def test_multiple_distinct_codes_in_supplier_scope_are_ambiguous(self) -> None:
        text = source().replace("Одержувач послуг", "Невизначена сторона")
        self.assertEqual(self.check(text), ["supplier_role_evidence_ambiguous"])
        self.assertEqual(self.check(text, code=BUYER_CODE, iban=BUYER_IBAN), ["supplier_role_evidence_ambiguous"])
        self.assertIsNone(supplier_source_text(text))

    def test_supplier_block_preserves_spaced_iban(self) -> None:
        text = source().replace(SUPPLIER_IBAN, " ".join(SUPPLIER_IBAN[index:index + 4] for index in range(0, len(SUPPLIER_IBAN), 4)))
        self.assertEqual(self.check(text), [])

    def test_vat_number_is_not_truncated_into_company_code(self) -> None:
        text = source().replace(f"ЄДРПОУ: {SUPPLIER_CODE}", f"ІПН: {SUPPLIER_CODE}0000")
        self.assertIn("supplier_tax_id_mismatch_source", self.check(text))

    def test_company_code_followed_by_comma_and_vat_number_is_supported(self) -> None:
        text = source().replace(
            f"ЄДРПОУ: {SUPPLIER_CODE}",
            f"код за ЄДРПОУ {SUPPLIER_CODE}, ІПН {SUPPLIER_CODE}0000",
        ).replace(SUPPLIER_NAME, 'Товариство з обмеженою відповідальністю\n«Синтетичний Постач»')
        self.assertEqual(self.check(text), [])

    def test_tax_label_does_not_turn_a_decimal_into_company_code(self) -> None:
        for separator in (".", ","):
            with self.subTest(separator=separator):
                text = source().replace(f"ЄДРПОУ: {SUPPLIER_CODE}", f"Код {SUPPLIER_CODE}{separator}99")
                self.assertIn("supplier_tax_id_mismatch_source", self.check(text))


if __name__ == "__main__":
    unittest.main()
