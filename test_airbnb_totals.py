import unittest

import airbnb_imap_bot_fixed_v2 as bot


class AirbnbHostTotalTests(unittest.TestCase):
    def parse_total(self, content):
        text = bot.html_to_text(content)
        return bot.get_airbnb_host_total(content, text)

    def test_two_totals_uses_host_total_after_host_fee(self):
        content = """
        <table>
          <tr><td>Total (USD)</td><td>$462.00</td></tr>
          <tr><td>$104.00 x 4 noches</td><td>$416.00</td></tr>
          <tr><td>Tarifa de limpieza</td><td>$46.00</td></tr>
          <tr><td>Tarifa por servicio para el huésped</td><td>$0.00</td></tr>
          <tr><td>Tarifa de la habitación por 4 noches</td><td>$416.00</td></tr>
          <tr><td>Tarifa de limpieza</td><td>$46.00</td></tr>
          <tr><td>Tarifa de servicio para anfitriones (15.5 %)</td><td>-$71.61</td></tr>
          <tr><td>Total (USD)</td><td>$390.39</td></tr>
        </table>
        """
        self.assertEqual(self.parse_total(content), 390.39)

    def test_ganas_has_highest_priority(self):
        content = """
        Precio total de la estancia
        490,00 $
        Gastos de limpieza
        52,00 $
        Comisión de servicio del anfitrión (15.5 %)
        -84,01 $
        Ganas
        457,99 $
        """
        self.assertEqual(self.parse_total(content), 457.99)

    def test_reconstructs_net_when_contextual_total_is_missing(self):
        content = """
        Tarifa de la habitación por 4 noches
        $416.00
        Tarifa de limpieza
        $46.00
        Tarifa de servicio para anfitriones (15.5 %)
        -$71.61
        """
        self.assertEqual(self.parse_total(content), 390.39)

    def test_single_legacy_total_still_works(self):
        content = """
        Detalle
        Total (USD)
        $275.50
        """
        self.assertEqual(self.parse_total(content), 275.50)


if __name__ == "__main__":
    unittest.main()
