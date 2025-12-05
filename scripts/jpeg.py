class Jpeg():
    def sv(self, params):
        return f"""
            read_verilog -sv -Icanon/OpenROAD-flow-scripts/flow/designs/src/jpeg/include canon/OpenROAD-flow-scripts/flow/designs/src/jpeg/*.v
            hierarchy -top jpeg_encoder
        """

__all__ = [Jpeg]