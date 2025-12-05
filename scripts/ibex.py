class Ibex():
    def sv(self, params):
        return f"""
            read_verilog -sv -Icanon/OpenROAD-flow-scripts/flow/designs/src/chameleon/ibex/include canon/OpenROAD-flow-scripts/flow/designs/src/chameleon/ibex/*.v
            hierarchy -top ibex_core
        """

__all__ = [Ibex]