class Fft():
    def partial_sv(self, params):
        return f"""
            read_verilog -sv verilog/SdfUnit.v verilog/SdfUnit2.v verilog/Butterfly.v verilog/DelayBuffer.v verilog/Multiply.v verilog/Twiddle64.v
            hierarchy -top FFT -chparam WIDTH {params.width}
        """

class Fft1024(Fft):
    def sv(self, params):
        return """
            read_verilog -sv verilog/FFT1024_32B.v
        """ + super().partial_sv(params)

class Fft64(Fft):
    def sv(self, params):
        return """
            read_verilog -sv verilog/FFT64.v
        """ + super().partial_sv(params)

__all__ = [Fft1024, Fft64]